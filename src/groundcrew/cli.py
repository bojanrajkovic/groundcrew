"""Command-line interface: daemon, status, add, remove, clean, logs."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from groundcrew import claude_state, gitops, supervise
from groundcrew.config import (
    EX_CONFIG,
    Config,
    ConfigError,
    load,
    load_registry,
    repo_path,
    save_registry,
    state_dir,
)
from groundcrew.daemon import FleetState, run_daemon
from groundcrew.supervise import RepoState


def _needs_same_dir(repo: Path) -> str:
    """The refusal, carrying the resolved path the override has to be keyed by."""
    return (
        f'refused {repo}: not a git repository, and spawn is "worktree", which needs one.\n'
        f"add this to config.toml, then retry:\n\n"
        f'    [repos."{repo}"]\n'
        f'    spawn = "same-dir"\n'
    )


def cmd_add(cfg: Config, paths: list[str]) -> int:
    registry = load_registry()
    to_add: list[Path] = []
    for raw in paths:
        repo = repo_path(raw)
        if not repo.is_dir():
            print(f"skip {repo}: not a directory")
            continue
        if gitops.is_git_repo(repo):
            if not gitops.has_remote(repo):
                print(f"note {repo}: no git remote; pulls will be skipped")
        elif cfg.for_repo(repo).spawn == "worktree":
            print(_needs_same_dir(repo))
            continue
        else:
            print(f"note {repo}: not a git repository; freshness pulls are skipped")
        to_add.append(repo)
    if not to_add:
        return 1
    seeded = claude_state.seed_trust(to_add)
    save_registry(registry + to_add)
    for repo in to_add:
        trust = "trust seeded" if repo in seeded else "already trusted"
        already = " (already registered)" if repo in registry else ""
        print(f"added {repo} — {trust}{already}")
    print("the daemon picks these up within 30 seconds")
    return 0


def cmd_remove(paths: list[str]) -> int:
    registry = load_registry()
    remaining = list(registry)
    for raw in paths:
        repo = repo_path(raw)
        if repo not in remaining:
            print(f"skip {repo}: not registered")
            continue
        remaining.remove(repo)
        print(f"removed {repo} — the daemon retires its supervisor once its sessions go quiet")
    if remaining != registry:
        save_registry(remaining)
    return 0


def _ago(then: float, now: float) -> str:
    if then <= 0:
        return "never"
    hours, minutes = divmod(int((now - then) / 60), 60)
    return f"{hours}h{minutes:02d}m ago" if hours else f"{minutes}m ago"


def _print_repo_row(
    path_str: str,
    info: RepoState,
    sessions: list[claude_state.SessionInfo],
    root: str,
    now: float,
) -> None:
    repo = Path(path_str)
    if info.alive():
        sup = f"up {info.pid}"
    elif info.backoff_until > now:
        sup = f"backoff {int((info.backoff_until - now) / 60)}m"
    else:
        sup = "DOWN"
    repo_sessions = claude_state.rc_sessions_for(repo, sessions)
    if repo_sessions:
        quiet_min = claude_state.quiet_minutes(repo_sessions, now)
        sess = f"{len(repo_sessions)} ({quiet_min:.0f}m quiet)"
    else:
        sess = "0"
    pull = f"{info.last_pull_kind or '-'} {_ago(info.last_pull_at, now)}"
    name = path_str.removeprefix(root)
    print(f"{name:<34} {sup:<10} {info.version or '?':<10} {sess:<12} {pull:<22}")
    for warning in info.warnings:
        print(f"{'':<34} ⚠ {warning}")
    for wt in gitops.spawned_worktrees(repo):
        if wt.dirty_files:
            print(
                f"{'':<34} ● dirty worktree {wt.path.name}: "
                f"{wt.dirty_files} file(s), {wt.age_days:.0f}d old"
            )


def cmd_status(cfg: Config) -> int:
    state_path = state_dir() / "state.json"
    if not state_path.exists():
        print("no state file — is the daemon running? (systemctl --user status groundcrew)")
        return 1
    try:
        state = FleetState.model_validate_json(state_path.read_text())
    except ValidationError as exc:
        print(f"state file unreadable (daemon version mismatch?): {exc}", file=sys.stderr)
        return 1
    now = time.time()
    print(f"claude {state.binary_version} · state updated {_ago(state.updated_at, now)}")
    if state.last_update_result:
        print(f"last nightly update: {state.last_update_result}")

    sessions = claude_state.live_sessions()
    root = str(cfg.root) + "/"
    header = f"{'REPO':<34} {'SUP':<10} {'VER':<10} {'SESS':<12} {'LAST PULL':<22}"
    print()
    print(header)
    for path_str in sorted(state.repos):
        _print_repo_row(path_str, state.repos[path_str], sessions, root, now)

    unmatched = sorted(str(p) for p in cfg.overrides if str(p) not in state.registered)
    for path_str in unmatched:
        print(f"⚠ config override for unregistered repo: {path_str}")

    if state.unregistered:
        print()
        print("not managed (register with `groundcrew add`):")
        for path_str in state.unregistered:
            print(f"  {path_str.removeprefix(root)}")
    return 0


def cmd_clean(raw: str) -> int:
    repo = repo_path(raw)
    worktrees = gitops.spawned_worktrees(repo)
    if not worktrees:
        print(f"no spawned worktrees under {repo}")
        return 0
    for wt in worktrees:
        parts = []
        if wt.dirty_files:
            parts.append(f"{wt.dirty_files} dirty file(s)")
        if wt.unmerged_commits:
            parts.append(f"{wt.unmerged_commits} commit(s) not on the main checkout's HEAD")
        state = ", ".join(parts) or "clean"
        print(f"\n{wt.path.name} — branch {wt.branch}, {state}, {wt.age_days:.0f}d old")
        if wt.dirty_files:
            print(gitops.worktree_dirty_listing(wt))
        if wt.unmerged_commits:
            print(gitops.worktree_unmerged_listing(repo, wt))
        prompt = (
            f"DELETE this worktree, its branch, and the {wt.unmerged_commits} commit(s) "
            "shown above (unrecoverable outside the reflog)? [y/N] "
            if wt.unmerged_commits
            else "delete this worktree (and its branch)? [y/N] "
        )
        answer = input(prompt).strip().lower()
        if answer != "y":
            print("kept")
            continue
        error = gitops.remove_worktree(repo, wt)
        print(f"failed: {error}" if error else "deleted")
    return 0


def cmd_logs(raw: str, lines: int, *, verbatim: bool) -> int:
    repo = repo_path(raw)
    path = supervise.log_path(repo)
    if not path.exists():
        print(f"no supervisor log for {repo} — has it ever been spawned?", file=sys.stderr)
        return 1
    # Deduplication needs the whole file anyway: what a tail would cut is the
    # first sighting of a line the visible frames go on repeating. Read the
    # retired generation first, or a spawn that just rotated would answer for
    # the fleet's whole history with whatever the new supervisor has said.
    retired = supervise.previous_log(path)
    text = (retired.read_text(errors="replace") if retired.exists() else "") + path.read_text(
        errors="replace"
    )
    body = text.splitlines() if verbatim else list(supervise.readable_log(text))
    for line in body[-lines:]:
        print(line)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="groundcrew", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="run the supervision daemon (systemd entry point)")
    sub.add_parser("status", help="show fleet state")
    p_add = sub.add_parser("add", help="trust + register directories")
    p_add.add_argument("paths", nargs="+")
    p_remove = sub.add_parser("remove", help="unregister repositories")
    p_remove.add_argument("paths", nargs="+")
    p_clean = sub.add_parser("clean", help="interactively delete spawned worktrees")
    p_clean.add_argument("repo")
    p_logs = sub.add_parser("logs", help="read a repo's supervisor log")
    p_logs.add_argument("repo")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="tail this many (default 50)")
    p_logs.add_argument(
        "--raw", action="store_true", help="keep the control codes and repeated frames"
    )
    args = parser.parse_args()

    try:
        cfg = load()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EX_CONFIG

    if args.command == "daemon":
        run_daemon(cfg)  # runs until signalled, so it is not one of the table's calls
        return 0
    # argparse refuses anything not registered above, so a missing key is a bug.
    commands: dict[str, Callable[[], int]] = {
        "status": lambda: cmd_status(cfg),
        "add": lambda: cmd_add(cfg, args.paths),
        "remove": lambda: cmd_remove(args.paths),
        "clean": lambda: cmd_clean(args.repo),
        "logs": lambda: cmd_logs(args.repo, args.lines, verbatim=args.raw),
    }
    return commands[args.command]()


if __name__ == "__main__":
    sys.exit(main())
