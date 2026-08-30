"""Command-line interface: daemon, status, add, remove, clean, logs."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, ValidationError

from groundcrew import claude_state, gitops, supervise
from groundcrew.config import (
    EX_CONFIG,
    Config,
    ConfigError,
    PermissionMode,
    Spawn,
    _RegistryEntry,
    effective,
    load,
    load_registry,
    repo_path,
    save_registry,
    state_dir,
)
from groundcrew.daemon import FleetState, run_daemon
from groundcrew.migrate import migrate_config
from groundcrew.supervise import RepoState


def _needs_same_dir(repo: Path) -> str:
    """The refusal: a chosen spawn mode and the directory contradict each other."""
    return f'refused {repo}: not a git repository, and spawn = "worktree" needs one.'


def cmd_add(cfg: Config, paths: list[str], settings: dict[str, object]) -> int:
    """Register directories, creating or updating each one's entry from `settings`.

    Only the settings named are written, so adding a directory that is already
    registered changes nothing else about it.
    """
    registry = load_registry()
    known = {entry.path: entry for entry in registry}
    to_add: list[_RegistryEntry] = []
    inferred: set[Path] = set()
    for raw in paths:
        repo = repo_path(raw)
        if not repo.is_dir():
            print(f"skip {repo}: not a directory")
            continue
        was = known.get(repo, _RegistryEntry(path=repo))
        # The flags are the boundary, so they get validated as an entry would be.
        entry = _RegistryEntry.model_validate({**was.model_dump(), **settings})
        if gitops.is_git_repo(repo):
            if not gitops.has_remote(repo):
                print(f"note {repo}: no git remote; pulls will be skipped")
        elif entry.spawn == "worktree":
            # Set on this entry, by the flag just passed or by a human editing
            # the file. Either way somebody chose it, so say no rather than
            # quietly rewriting the choice.
            print(_needs_same_dir(repo))
            continue
        elif effective(cfg.defaults, entry).spawn == "worktree":
            # Only the global default asked for worktree, and it cannot work
            # here — infer same-dir into the entry, or the next pass repeats it.
            entry = entry.model_copy(update={"spawn": "same-dir"})
            inferred.add(repo)
        else:
            print(f"note {repo}: not a git repository; freshness pulls are skipped")
        to_add.append(entry)
    if not to_add:
        return 1
    seeded = claude_state.seed_trust([e.path for e in to_add])
    # save_registry keys on the path and takes the last write, so these replace
    # the entries they were built from and append the ones that are new.
    save_registry(registry + to_add)
    for entry in to_add:
        repo = entry.path
        why = 'not a git repository, so spawn = "same-dir"; ' if repo in inferred else ""
        trust = "trust seeded" if repo in seeded else "already trusted"
        if repo not in known:
            state = ""
        else:
            state = " (settings updated)" if entry != known[repo] else " (already registered)"
        print(f"added {repo} — {why}{trust}{state}")
    print("the daemon picks these up within 30 seconds")
    return 0


def cmd_remove(paths: list[str]) -> int:
    registry = load_registry()
    remaining = list(registry)
    for raw in paths:
        repo = repo_path(raw)
        entry = next((e for e in remaining if e.path == repo), None)
        if entry is None:
            print(f"skip {repo}: not registered")
            continue
        remaining.remove(entry)
        print(f"removed {repo} — the daemon retires its supervisor once its sessions go quiet")
    if remaining != registry:
        save_registry(remaining)
    return 0


def _ago(then: float, now: float) -> str:
    if then <= 0:
        return "never"
    hours, minutes = divmod(max(0, int((now - then) / 60)), 60)
    return f"{hours}h{minutes:02d}m ago" if hours else f"{minutes}m ago"


def _column_widths(headers: list[str], rows: list[list[str]]) -> list[int]:
    """Each column's width is its own longest value, so nothing overflows into the next one."""
    return [max(len(col[i]) for col in [headers, *rows]) for i in range(len(headers))]


def _fmt_row(cols: list[str], widths: list[int]) -> str:
    """Pad every column but the last, which would only gain trailing whitespace from it."""
    return " ".join([*(c.ljust(w) for c, w in zip(cols[:-1], widths[:-1], strict=True)), cols[-1]])


class RepoRow(BaseModel):
    """One repo's headline fields — shared by the table and `--json`.

    Carries raw data (pid, epoch seconds), not pre-rendered display strings, so a
    `--json` consumer can compare/alert on it without re-parsing human text.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    state: Literal["up", "backoff", "down"]
    pid: int | None  # set when state == "up"
    backoff_seconds: int | None  # set when state == "backoff"
    version: str | None
    session_count: int
    quiet_minutes: float | None  # None when session_count == 0
    last_pull_kind: str | None
    last_pull_at: float | None  # epoch seconds; None if never pulled


def _repo_row(
    path_str: str,
    info: RepoState,
    sessions: list[claude_state.SessionInfo],
    now: float,
) -> RepoRow:
    pid: int | None = None
    backoff_seconds: int | None = None
    state: Literal["up", "backoff", "down"]
    if info.alive():
        state, pid = "up", info.pid
    elif info.backoff_until > now:
        state, backoff_seconds = "backoff", max(0, int(info.backoff_until - now))
    else:
        state = "down"
    repo_sessions = claude_state.rc_sessions_for(Path(path_str), sessions)
    quiet_min = claude_state.quiet_minutes(repo_sessions, now) if repo_sessions else None
    return RepoRow(
        path=path_str,
        state=state,
        pid=pid,
        backoff_seconds=backoff_seconds,
        version=info.version,
        session_count=len(repo_sessions),
        quiet_minutes=max(0.0, quiet_min) if quiet_min is not None else None,
        last_pull_kind=info.last_pull_kind or None,
        last_pull_at=info.last_pull_at if info.last_pull_at > 0 else None,
    )


def _status_cols(row: RepoRow, root: str, now: float) -> list[str]:
    if row.state == "up":
        sup = f"up {row.pid}"
    elif row.state == "backoff":
        sup = f"backoff {(row.backoff_seconds or 0) // 60}m"
    else:
        sup = "DOWN"
    if row.quiet_minutes is not None:
        sess = f"{row.session_count} ({row.quiet_minutes:.0f}m quiet)"
    else:
        sess = "0"
    pull = f"{row.last_pull_kind or '-'} {_ago(row.last_pull_at or 0, now)}"
    return [row.path.removeprefix(root), sup, row.version or "?", sess, pull]


def _print_repo_annotations(repo_path: str, warnings: list[str], indent: int) -> None:
    for warning in warnings:
        print(f"{'':<{indent}} ⚠ {warning}")
    for wt in gitops.spawned_worktrees(Path(repo_path)):
        if wt.dirty_files:
            print(
                f"{'':<{indent}} ● dirty worktree {wt.path.name}: "
                f"{wt.dirty_files} file(s), {wt.age_days:.0f}d old"
            )


def cmd_status(cfg: Config, *, as_json: bool = False) -> int:
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
    sessions = claude_state.live_sessions()
    displays = [
        (_repo_row(path_str, state.repos[path_str], sessions, now), state.repos[path_str].warnings)
        for path_str in sorted(state.repos)
    ]

    if as_json:
        print(json.dumps([row.model_dump(mode="json") for row, _ in displays], indent=2))
        return 0

    print(f"claude {state.binary_version} · state updated {_ago(state.updated_at, now)}")
    if state.last_update_result:
        print(f"last nightly update: {state.last_update_result}")

    root = str(cfg.root) + "/"
    headers = ["REPO", "SUP", "VER", "SESS", "LAST PULL"]
    table_rows = [_status_cols(row, root, now) for row, _ in displays]
    widths = _column_widths(headers, table_rows)
    print()
    print(_fmt_row(headers, widths))
    for (row, warnings), cols in zip(displays, table_rows, strict=True):
        print(_fmt_row(cols, widths))
        _print_repo_annotations(row.path, warnings, widths[0])

    if state.unregistered:
        print()
        print("not managed (register with `groundcrew add`):")
        for path_str in state.unregistered:
            print(f"  {path_str.removeprefix(root)}")
    return 0


class SessionRow(BaseModel):
    """One live session — shared by the table and `--json`."""

    model_config = ConfigDict(frozen=True)

    repo: str
    worktree: str | None  # None for a same-dir session (no spawned worktree)
    address: str | None  # matches the name `ListAgents` shows for this peer
    title: str | None  # best-effort; most sessions never get one
    session_id: str
    pid: int
    branch: str | None


def _session_rows(state: FleetState, sessions: list[claude_state.SessionInfo]) -> list[SessionRow]:
    rows: list[SessionRow] = []
    for path_str in sorted(state.repos):
        repo = Path(path_str)
        by_cwd = {wt.path: wt for wt in gitops.spawned_worktrees(repo)}
        for s in claude_state.rc_sessions_for(repo, sessions):
            wt = by_cwd.get(s.cwd)
            rows.append(
                SessionRow(
                    repo=path_str,
                    worktree=str(wt.path) if wt else None,
                    address=s.address,
                    title=claude_state.session_title(s),
                    session_id=s.session_id,
                    pid=s.pid,
                    branch=wt.branch if wt else gitops.current_branch(repo),
                )
            )
    return rows


def _session_cols(row: SessionRow, root: str) -> list[str]:
    wt_name = Path(row.worktree).name if row.worktree else "-"
    return [
        row.repo.removeprefix(root),
        wt_name,
        row.address or "-",
        row.branch or "-",
        row.title or "",
    ]


def cmd_sessions(cfg: Config, *, as_json: bool = False) -> int:
    state_path = state_dir() / "state.json"
    if not state_path.exists():
        print("no state file — is the daemon running? (systemctl --user status groundcrew)")
        return 1
    try:
        state = FleetState.model_validate_json(state_path.read_text())
    except ValidationError as exc:
        print(f"state file unreadable (daemon version mismatch?): {exc}", file=sys.stderr)
        return 1

    rows = _session_rows(state, claude_state.live_sessions())

    if as_json:
        print(json.dumps([row.model_dump(mode="json") for row in rows], indent=2))
        return 0

    if not rows:
        print("no live sessions")
        return 0
    root = str(cfg.root) + "/"
    headers = ["REPO", "WORKTREE", "ADDRESS", "BRANCH", "TITLE"]
    table_rows = [_session_cols(row, root) for row in rows]
    widths = _column_widths(headers, table_rows)
    print(_fmt_row(headers, widths))
    for cols in table_rows:
        print(_fmt_row(cols, widths))
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


def _flags(args: argparse.Namespace) -> dict[str, object]:
    """The per-repo settings the flags actually named; every other one stays unset."""
    post_pull = [] if args.no_post_pull else args.post_pull
    return {
        k: v
        for k, v in {
            "spawn": args.spawn,
            "capacity": args.capacity,
            "permission_mode": args.permission_mode,
            "create_session_in_dir": args.create_session_in_dir,
            "post_pull": post_pull,
        }.items()
        if v is not None
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="groundcrew", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="run the supervision daemon (systemd entry point)")
    p_status = sub.add_parser("status", help="show fleet state")
    p_status.add_argument("--json", action="store_true", help="emit headline fields as JSON")
    p_sessions = sub.add_parser("sessions", help="list supervised repos' live sessions")
    p_sessions.add_argument("--json", action="store_true", help="emit session rows as JSON")
    p_add = sub.add_parser(
        "add", help="trust + register directories, and set their per-directory settings"
    )
    p_add.add_argument("paths", nargs="+")
    # Every default is None: only a flag that was passed reaches the entry, so
    # adding a directory never stamps the globals into repos.toml.
    p_add.add_argument(
        "--spawn",
        choices=get_args(Spawn),
        default=None,
        help="how sessions get their working directory",
    )
    p_add.add_argument(
        "--capacity", type=int, default=None, help="concurrent sessions the supervisor accepts"
    )
    p_add.add_argument(
        "--permission-mode",
        choices=get_args(PermissionMode),
        default=None,
        help="permission mode this directory's sessions start in",
    )
    p_add.add_argument(
        "--create-session-in-dir",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="pre-create a session in the directory itself",
    )
    hook = p_add.add_mutually_exclusive_group()
    # argparse turns a ValueError from `type` into a usage error, so an
    # unbalanced quote is reported against the flag rather than raised.
    hook.add_argument(
        "--post-pull",
        metavar="CMD",
        type=shlex.split,
        help='run after a pull, e.g. "mise install"',
    )
    hook.add_argument("--no-post-pull", action="store_true", help="disable the hook for this repo")
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

    # Both files are read lazily — config.toml here, repos.toml inside the
    # commands that touch it — so one handler covers both. The migration goes
    # first: the file schema has no `repos` key, so a config still holding the
    # old per-repo tables fails to load rather than reaching it.
    try:
        migrate_config()
        cfg = load()
        if args.command == "daemon":
            run_daemon(cfg)  # runs until signalled, so it is not one of the table's calls
            return 0
        # argparse refuses anything not registered above, so a missing key is a bug.
        commands: dict[str, Callable[[], int]] = {
            "status": lambda: cmd_status(cfg, as_json=args.json),
            "sessions": lambda: cmd_sessions(cfg, as_json=args.json),
            "add": lambda: cmd_add(cfg, args.paths, _flags(args)),
            "remove": lambda: cmd_remove(args.paths),
            "clean": lambda: cmd_clean(args.repo),
            "logs": lambda: cmd_logs(args.repo, args.lines, verbatim=args.raw),
        }
        return commands[args.command]()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return EX_CONFIG


if __name__ == "__main__":
    sys.exit(main())
