# groundcrew

Self-hosted Claude Code cloud environments. groundcrew keeps one
`claude remote-control --spawn worktree` process alive in every registered
repository, so any repo can be picked up instantly from claude.ai or the
mobile app — and keeps the fleet fresh (git pulls, tool installs, Claude Code
upgrades) without ever losing an in-flight session.

```
groundcrew add <path>...   # seed workspace trust + register (one deliberate step)
groundcrew remove <path>   # unregister; supervisor retires once its sessions go quiet
groundcrew status          # fleet table: supervisors, versions, sessions, pulls, warnings
groundcrew clean <repo>    # interactively delete spawned worktrees; shows dirty files
                           # AND unmerged commits before asking
groundcrew daemon          # the long-running process (service entry point)
```

The registry is `~/.config/groundcrew/repos.toml`, maintained by
`add`/`remove`; each entry carries the settings that differ from the globals in
`config.toml` (see [Configuration](docs/configuration.md)). New clones never
join silently — `status` lists unregistered repos it finds under the projects
root.

## Documentation

- **[Architecture](docs/architecture.md)** — how the daemon, supervisors, and
  session engines fit together, why restarts never lose work, and the
  reconcile/tick/nightly loop.
- **[Install](docs/install.md)** — `uv tool install git+…`, Linux/systemd and
  macOS/launchd setup, and the upgrade runbook.
- **[Configuration](docs/configuration.md)** — global settings, per-repo
  settings, notifier commands, and the post-pull hook.

## Development

```sh
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
uv run pytest
```

MIT licensed.
