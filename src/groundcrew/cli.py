"""Command-line interface: daemon, status, add, remove, clean."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from groundcrew import claude_state, gitops
from groundcrew.config import load_registry, projects_root, save_registry, state_dir
from groundcrew.daemon import run_daemon


def cmd_add(paths: list[str]) -> int:
    registry = load_registry()
    to_add: list[Path] = []
    for raw in paths:
        repo = Path(raw).expanduser().resolve()
        if not (repo / ".git").exists():
            print(f"skip {repo}: not a git repository")
            continue
        if not gitops.has_remote(repo):
            print(f"note {repo}: no git remote; pulls will be skipped")
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
        repo = Path(raw).expanduser().resolve()
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
    info: dict[str, object],
    sessions: list[claude_state.SessionInfo],
    root: str,
    now: float,
) -> None:
    repo = Path(path_str)
    pid = info.get("pid")
    alive = isinstance(pid, int) and claude_state.proc_start(pid) == info.get("proc_start")
    backoff_until = info.get("backoff_until")
    if alive:
        sup = f"up {pid}"
    elif isinstance(backoff_until, (int, float)) and backoff_until > now:
        sup = f"backoff {int((backoff_until - now) / 60)}m"
    else:
        sup = "DOWN"
    repo_sessions = claude_state.rc_sessions_for(repo, sessions)
    if repo_sessions:
        quiet_min = min((now - claude_state.last_activity(s)) / 60 for s in repo_sessions)
        sess = f"{len(repo_sessions)} ({quiet_min:.0f}m quiet)"
    else:
        sess = "0"
    pull_at = info.get("last_pull_at")
    pull_at = pull_at if isinstance(pull_at, (int, float)) else 0.0
    pull = f"{info.get('last_pull_kind') or '-'} {_ago(pull_at, now)}"
    name = path_str.removeprefix(root)
    print(f"{name:<34} {sup:<10} {info.get('version') or '?':<10} {sess:<12} {pull:<22}")
    warnings = info.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            print(f"{'':<34} ⚠ {warning}")
    for wt in gitops.spawned_worktrees(repo):
        if wt.dirty_files:
            print(
                f"{'':<34} ● dirty worktree {wt.path.name}: "
                f"{wt.dirty_files} file(s), {wt.age_days:.0f}d old"
            )


def cmd_status() -> int:
    state_path = state_dir() / "state.json"
    if not state_path.exists():
        print("no state file — is the daemon running? (systemctl --user status groundcrew)")
        return 1
    state = json.loads(state_path.read_text())
    now = time.time()
    updated = _ago(float(state.get("updated_at", 0)), now)
    print(f"claude {state.get('binary_version')} · state updated {updated}")
    if state.get("last_update_result"):
        print(f"last nightly update: {state['last_update_result']}")

    sessions = claude_state.live_sessions()
    root = str(projects_root()) + "/"
    repos: dict[str, dict[str, object]] = state.get("repos", {})
    header = f"{'REPO':<34} {'SUP':<10} {'VER':<10} {'SESS':<12} {'LAST PULL':<22}"
    print()
    print(header)
    for path_str in sorted(repos):
        _print_repo_row(path_str, repos[path_str], sessions, root, now)

    unregistered = state.get("unregistered", [])
    if isinstance(unregistered, list) and unregistered:
        print()
        print("not managed (register with `groundcrew add`):")
        for path_str in unregistered:
            print(f"  {str(path_str).removeprefix(root)}")
    return 0


def cmd_clean(raw: str) -> int:
    repo = Path(raw).expanduser().resolve()
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
            diff = gitops.run_git(wt.path, "status", "--short")
            print(diff.stdout.rstrip())
        if wt.unmerged_commits and wt.branch:
            commits = gitops.run_git(repo, "log", "--oneline", f"HEAD..{wt.branch}")
            print(commits.stdout.rstrip())
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
        removed = gitops.run_git(repo, "worktree", "remove", "--force", str(wt.path))
        if removed.returncode != 0:
            print(f"failed: {removed.stderr.strip()}")
            continue
        if wt.branch:
            gitops.run_git(repo, "branch", "-D", wt.branch)
        print("deleted")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="groundcrew", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="run the supervision daemon (systemd entry point)")
    sub.add_parser("status", help="show fleet state")
    p_add = sub.add_parser("add", help="trust + register repositories")
    p_add.add_argument("paths", nargs="+")
    p_remove = sub.add_parser("remove", help="unregister repositories")
    p_remove.add_argument("paths", nargs="+")
    p_clean = sub.add_parser("clean", help="interactively delete spawned worktrees")
    p_clean.add_argument("repo")
    args = parser.parse_args()

    if args.command == "daemon":
        run_daemon()
        return 0
    if args.command == "status":
        return cmd_status()
    if args.command == "add":
        return cmd_add(args.paths)
    if args.command == "remove":
        return cmd_remove(args.paths)
    if args.command == "clean":
        return cmd_clean(args.repo)
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
