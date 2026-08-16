# Install

## Linux (systemd) — the supported path

```sh
uv tool install --editable ~/Projects/groundcrew
mkdir -p ~/.config/systemd/user ~/.config/groundcrew
cp ~/Projects/groundcrew/systemd/groundcrew.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now groundcrew
```

One-time host preparation:

- `loginctl enable-linger <user>` so the unit runs without a login session
- `mise settings set trusted_config_paths ~/Projects` so headless
  `mise install` never blocks on a trust prompt
- optional `~/.config/groundcrew/env` (mode 0600) for Pushover and overrides:

```
PUSHOVER_TOKEN=...
PUSHOVER_USER=...
# SSH_AUTH_SOCK=/run/user/1000/... if ssh-agent auth is needed for pulls
```

Pushover pings on: crash loops, three consecutive pull failures for a repo,
`mise install` failures, `claude update` failures, and (the one success ping)
a completed fleet-wide version rollout. Without credentials the daemon logs
the suppressed notification and carries on.

## Configuration

Settings live in `~/.config/groundcrew/config.toml` (XDG-aware), which
groundcrew never writes; the registry lives beside it as
`~/.config/groundcrew/repos.toml`, maintained by `groundcrew add`/`remove`.
No config file means the built-in defaults — every key below shows its
default:

```toml
root = "~/Projects"          # where repos are discovered

[timing]
quiet_seconds = 900          # transcript-quiet window before restarts
tick_seconds = 3600          # freshness pull cadence
nightly_hour = 4             # local hour of the `claude update` backstop

[hooks]
post_pull_timeout = 600
```

Validation is strict: an unknown key, a wrong type, or a bad value anywhere
fails the load with the exact key path named, and the daemon exits with
status 78 (`EX_CONFIG`) — the systemd unit marks that exit
restart-preventing, so a typo halts the service instead of restart-looping
it.

Supervisor launch settings live in `[claude]` — `spawn`
(`"worktree"`/`"same-dir"`), `capacity`, `permission_mode` — with per-repo
overrides in `[repos."<path>"]` tables (any `[claude]` key except `bin`,
plus `post_pull`). Changing them takes effect through drift: restart the
daemon and each affected supervisor converges once its sessions are quiet.
The `[notify]` and `[hooks].post_pull` tables are validated but not yet
acted on; notifier commands and the post-pull hook are being wired in
behind this format.

Environment variables override everything, mainly for tests:
`GROUNDCREW_ROOT`, `GROUNDCREW_CONFIG_DIR`, `GROUNDCREW_REGISTRY`,
`GROUNDCREW_STATE`, `GROUNDCREW_CLAUDE_HOME`, `GROUNDCREW_CLAUDE_JSON`.
Protective tunables (spawn ramp, crash breaker, backoff, alert thresholds)
are deliberately constants, not config.

## macOS (launchd) — not yet supported, here is what it would take

The daemon is currently **Linux-only**, and the blocker is not launchd — it is
`/proc`. Three load-bearing mechanisms read it directly:

1. **Orphan adoption** (`supervise.find_orphans`) scans `/proc/*/cmdline` and
   `/proc/*/cwd` to re-adopt supervisors across daemon restarts.
2. **PID-reuse guarding** (`claude_state.proc_start`) reads the kernel start
   time from `/proc/<pid>/stat` so a recycled PID is never mistaken for a
   live supervisor or session.
3. **Version detection** (`claude_state.process_version`) resolves
   `/proc/<pid>/exe` to find which binary a process is actually running.

macOS has no procfs; equivalents exist via `libproc`/`sysctl` (or the
`psutil` package, which wraps them portably: `psutil.Process.cmdline()`,
`.cwd()`, `.create_time()`, `.exe()`). Porting means swapping those three
functions onto psutil and taking it as the one runtime dependency.

With that done, the launchd mapping for a user LaunchAgent
(`~/Library/LaunchAgents/com.example.groundcrew.plist`) would be:

| systemd concept | launchd equivalent |
|---|---|
| `Restart=on-failure` | `KeepAlive` → `{ SuccessfulExit = false }` |
| `KillMode=process` (children survive) | `AbandonProcessGroup = true` |
| start on boot + linger | `RunAtLoad = true` — but see the login caveat below |
| journald | none: set `StandardOutPath`/`StandardErrorPath` to log files |
| `EnvironmentFile=` | none: inline `EnvironmentVariables` dict in the plist, or a wrapper script that sources the env file |
| minimal PATH fix | same `EnvironmentVariables` dict must carry PATH (mise shims + `~/.local/bin`) |

Remaining launchd complications, in honesty order:

- **No linger equivalent.** LaunchAgents run inside a login session; a
  headless Mac needs either an auto-login user or a system LaunchDaemon —
  and a LaunchDaemon runs as root outside the user context, which breaks
  Claude's per-user credentials and keychain. Practical answer: keep the
  user logged in.
- **Secrets in plists are world-readable-ish.** `EnvironmentVariables`
  containing the Pushover token sits in a plaintext plist; prefer the
  wrapper-script-sources-env-file approach with 0600 permissions.
- **Sleep is not a restart.** launchd does not re-run jobs on wake; the
  daemon's poll loop tolerates sleep fine, but hourly ticks slip by however
  long the lid was closed.
- `claude` and `mise` native paths differ (`~/.local/bin` still works if you
  use the native installer; Homebrew installs land elsewhere and
  `config.claude_bin()`'s native-first default would need the right path).
