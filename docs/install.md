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
- optional `~/.config/groundcrew/env` (mode 0600) for secrets the notifier
  needs and environment overrides:

```
PUSHOVER_TOKEN=...   # read by contrib/notify-pushover, not by groundcrew
PUSHOVER_USER=...
# SSH_AUTH_SOCK=/run/user/1000/... if ssh-agent auth is needed for pulls
```

## Notifications

groundcrew has no built-in alert providers (ADR 0001) — it runs the command
you configure, passing the alert title and message both as the two appended
arguments (`$1`, `$2`) and as `GROUNDCREW_TITLE` / `GROUNDCREW_MESSAGE` in
the environment. The notifier inherits the daemon's environment, so secrets
from the env file flow through. A failing or slow notifier (30 s timeout) is
logged and never retried; with no command configured, alerts are logged as
suppressed.

```toml
[notify]
command = ["/path/to/notifier"]
```

`contrib/notify-pushover` is a four-line Pushover notifier: copy it onto
your PATH (or point `command` at the file) and put `PUSHOVER_TOKEN` /
`PUSHOVER_USER` in the env file. Alerts fire on: crash loops, three
consecutive pull failures for a repo, post-pull hook failures, `claude
update` failures, and (the one success ping) a completed fleet-wide version
rollout.

## The post-pull hook

When a repo's default branch moves under an in-tree pull, groundcrew runs
the configured `post_pull` command in the repo root — toolchain refresh for
mise, pnpm, cargo, whatever fits. One command per scope; compose multi-step
setups in a script or `sh -c`. Per-repo tables replace the global wholesale,
and an explicit `post_pull = []` disables the hook for that repo. Parked
repos never run it (their working tree didn't change). Failures warn in
`status` and notify immediately.

```toml
[hooks]
post_pull = ["mise", "install"]
```

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
Notifications and the post-pull hook are user commands — see their sections
above.

Environment variables override everything, mainly for tests:
`GROUNDCREW_ROOT`, `GROUNDCREW_CONFIG_DIR`, `GROUNDCREW_REGISTRY`,
`GROUNDCREW_STATE`, `GROUNDCREW_CLAUDE_HOME`, `GROUNDCREW_CLAUDE_JSON`.
Protective tunables (spawn ramp, crash breaker, backoff, alert thresholds)
are deliberately constants, not config.

## macOS (launchd)

Process inspection — orphan adoption, PID-reuse guarding, version
detection — goes through `psutil` (the one runtime dependency), one code
path on both platforms, so the daemon itself runs on macOS. Session
PID-reuse is detected portably: a process created after the session's
recorded `startedAt` must be a recycler, so groundcrew never has to decode
the CLI's platform-specific `procStart` value.

The launchd mapping for a user LaunchAgent
(`~/Library/LaunchAgents/com.example.groundcrew.plist`):

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
