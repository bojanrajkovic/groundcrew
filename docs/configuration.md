# Configuration

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
restart-preventing (the launchd wrapper converts it to a stop), so a typo
halts the service instead of restart-looping it.

Supervisor launch settings live in `[claude]` — `spawn`
(`"worktree"`/`"same-dir"`), `capacity`, `permission_mode`,
`create_session_in_dir` (default `true`; `false` passes
`--no-create-session-in-dir` so no session is pre-created in the repo
root) — with per-repo overrides in `[repos."<path>"]` tables (any
`[claude]` key except `bin`, plus `post_pull`). Changing them takes effect through drift: restart the
daemon and each affected supervisor converges once its sessions are quiet.

Environment variables override everything, mainly for tests:
`GROUNDCREW_ROOT`, `GROUNDCREW_CONFIG_DIR`, `GROUNDCREW_REGISTRY`,
`GROUNDCREW_STATE`, `GROUNDCREW_CLAUDE_HOME`, `GROUNDCREW_CLAUDE_JSON`.
Protective tunables (spawn ramp, crash breaker, backoff, alert thresholds)
are deliberately constants, not config.

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
