# groundcrew

Self-hosted Claude Code cloud environments. groundcrew keeps one
`claude remote-control --spawn worktree` process alive in every registered
repository, so any repo can be picked up instantly from claude.ai or the
mobile app — and keeps the fleet fresh (git pulls, tool installs, Claude Code
upgrades) without ever losing an in-flight session.

## Architecture

```mermaid
flowchart TD
    SD[systemd --user\ngroundcrew.service\nKillMode=process] --> D[groundcrew daemon]
    D -->|spawn / adopt / restart| S1["claude remote-control\n(repo A)"]
    D -->|spawn / adopt / restart| S2["claude remote-control\n(repo B)"]
    D -->|...| SN["claude remote-control\n(repo N)"]
    S1 --> E1["session engine\n(repo root)"]
    S1 --> E2["session engine\n(.claude/worktrees/bridge-…)"]
    D -. state.json .-> ST[groundcrew status]
    D -. failures / rollouts .-> PO[Pushover]
```

Each supervisor connects its repo to a cloud environment; sessions requested
from claude.ai run as engine child processes, each in an isolated git worktree
under `<repo>/.claude/worktrees/`.

## Why restarts are safe

Session identity lives server-side. Restarting a supervisor in the same
directory reconnects the same cloud environment and the same sessions; worktree
sessions re-materialize lazily on next use. The CLI garbage-collects *clean*
worktrees on shutdown but preserves *dirty* ones (and their branches), so
uncommitted in-flight work survives any restart. All of this was verified
empirically against Claude Code 2.1.x before groundcrew was built.

Because remote-control engines never populate the session `status` field,
busy/idle is inferred: a repo is restartable once every one of its sessions has
had no transcript writes for 15 minutes.

## The loop

| Cadence | Work |
|---|---|
| 30 s | Reconcile: spawn missing supervisors, respawn dead ones (crash-loop breaker: 3 deaths in 10 min → 30 min backoff + Pushover), retire unregistered repos when quiet |
| hourly | Per repo: freshness pull → `mise install` when the default branch moved → version-drift restart when all sessions are quiet |
| nightly 04:00 | `claude update` as a backstop for the native auto-updater |

Spawns are rate-limited to a few per reconcile pass: registering many
environments at once trips the API's 429 rate limit and the rejected
supervisors exit. Cold starts, mass restarts, and fleet-wide drift updates all
ramp through this gate. Supervisors always run the native installer's binary
(`~/.local/bin/claude`), never a PATH/shim lookup — a repo's mise config may
pin its own `claude`, which must not become the supervisor.

Pull policy: on the default branch and clean → `git pull --ff-only`; parked on
another branch → `git fetch origin main:main` (updates the ref, never touches
the working tree); on the default branch but dirty → fetch only, plus a status
warning. Only modified *tracked* files count as dirty. A diverged local
default branch is classified as `diverged` — a warning for a human, not an
infrastructure failure, so it never triggers the consecutive-failure alert.

The daemon exits without touching its children (`KillMode=process`,
`start_new_session`); the next daemon instance re-adopts them from `/proc` by
cmdline + cwd, so groundcrew's own restarts never bounce the fleet.

## Commands

```
groundcrew add <path>...   # seed workspace trust + register (one deliberate step)
groundcrew remove <path>   # unregister; supervisor retires once its sessions go quiet
groundcrew status          # fleet table: supervisors, versions, sessions, pulls, warnings
groundcrew clean <repo>    # interactively delete spawned worktrees; shows dirty files
                           # AND unmerged commits before asking
groundcrew daemon          # the long-running process (systemd entry point)
```

The registry is `repos.toml` next to this README. New clones never join
silently — `status` lists unregistered repos it finds under the projects root.

## Install

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
- optional `~/.config/groundcrew/env` for Pushover and environment overrides:

```
PUSHOVER_TOKEN=...
PUSHOVER_USER=...
# SSH_AUTH_SOCK=/run/user/1000/... if ssh-agent auth is needed for pulls
```

Pushover pings on: crash loops, three consecutive pull failures for a repo,
`mise install` failures, `claude update` failures, and (the one success ping)
a completed fleet-wide version rollout.

## Configuration

Everything is environment-overridable, mainly for tests: `GROUNDCREW_ROOT`
(projects root), `GROUNDCREW_REGISTRY`, `GROUNDCREW_STATE`,
`GROUNDCREW_CLAUDE_HOME`, `GROUNDCREW_CLAUDE_JSON`.
Tunables (quiet window, cadences, backoff, spawn ramp) are constants in
`src/groundcrew/config.py`.

## Known limits

- Worktree sessions spawn from the repo's current HEAD, not the default
  branch — keep repos parked on their default branch; `status` warns when one
  isn't.
- A session doing a >15-minute silent tool run can be mistaken for idle and
  restarted; the conversation resumes and dirty files survive, but that turn's
  remaining work is lost.
- Resume after SIGKILL is untested; groundcrew always tries SIGTERM first and
  escalates only after 60 s.

## Development

```sh
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
uv run pytest
```
