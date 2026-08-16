"""Reading Claude Code's on-disk state: trust entries, live sessions, versions.

Facts this module leans on (verified against Claude Code 2.1.x):
- `~/.claude.json` holds `projects.<abs-path>.hasTrustDialogAccepted`; remote-control
  refuses to start in an untrusted directory.
- Each running session engine writes `~/.claude/sessions/<pid>.json` with its cwd,
  sessionId, version, and a `startedAt` epoch-milliseconds timestamp written just
  after the engine starts. The file is removed on clean shutdown but survives
  crashes, so liveness must be re-checked. PID reuse is detected by inequality:
  the engine held its PID continuously from startedAt until death, so any process
  now wearing that PID but created *after* startedAt is a recycler. (The file also
  records `procStart`, but its format is the CLI's platform-specific implementation
  detail — Linux jiffies today — so groundcrew never reads it.)
- The engines' `status` field is not populated for remote-control sessions, so
  busy/idle is inferred from transcript mtime (quiet-for-N-minutes heuristic).
- Transcripts live at `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`; we find
  them by globbing for the sessionId rather than re-implementing path encoding.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

from groundcrew.config import atomic_write, claude_bin, claude_home, claude_json_path

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def trusted_paths() -> set[str]:
    path = claude_json_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # Live sessions rewrite this file constantly; a torn read must degrade
        # (skip spawns this pass) rather than crash the daemon loop.
        return set()
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        return set()
    return {
        p
        for p, cfg in projects.items()
        if isinstance(cfg, dict) and cfg.get("hasTrustDialogAccepted") is True
    }


def seed_trust(repos: list[Path]) -> list[Path]:
    """Mark repos trusted in ~/.claude.json. Returns the repos that were newly seeded.

    One read-modify-rename pass. The file is also rewritten by live Claude sessions;
    a deliberate `groundcrew add` accepts that small race — the daemon itself never
    writes this file.
    """
    path = claude_json_path()
    data: dict[str, object] = json.loads(path.read_text()) if path.exists() else {}
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise TypeError(f"{path}: 'projects' is not an object")
    changed: list[Path] = []
    for repo in repos:
        entry = projects.setdefault(str(repo), {})
        if not isinstance(entry, dict):
            raise TypeError(f"{path}: projects[{str(repo)!r}] is not an object")
        if entry.get("hasTrustDialogAccepted") is not True:
            entry["hasTrustDialogAccepted"] = True
            changed.append(repo)
    if changed:
        atomic_write(path, json.dumps(data, indent=2))
    return changed


def proc_create_time(pid: int) -> float | None:
    """Kernel creation time of the process (epoch seconds), or None if it is gone.

    Deterministic for a given process, so exact equality against a previously
    recorded value is the PID-reuse-safe liveness check.
    """
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


@dataclass(frozen=True)
class SessionInfo:
    pid: int
    session_id: str
    cwd: Path
    started_at: float  # unix seconds
    version: str | None
    entrypoint: str | None = None  # "sdk-cli" for remote-control engines


def live_sessions() -> list[SessionInfo]:
    """Sessions with a metadata file AND a matching live process (stale files skipped)."""
    sessions_dir = claude_home() / "sessions"
    if not sessions_dir.is_dir():
        return []
    out: list[SessionInfo] = []
    for meta in sessions_dir.glob("*.json"):
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        session_id = data.get("sessionId")
        cwd = data.get("cwd")
        if not (isinstance(pid, int) and isinstance(session_id, str) and isinstance(cwd, str)):
            continue
        created = proc_create_time(pid)
        if created is None:
            continue  # process gone; crash leftover
        started_ms = data.get("startedAt")
        if isinstance(started_ms, (int, float)) and created > started_ms / 1000:
            # The engine existed before startedAt was written; a process created
            # after it must have picked up the PID after the engine died.
            continue
        version = data.get("version")
        entrypoint = data.get("entrypoint")
        out.append(
            SessionInfo(
                pid=pid,
                session_id=session_id,
                cwd=Path(cwd),
                started_at=started_ms / 1000 if isinstance(started_ms, (int, float)) else 0.0,
                version=version if isinstance(version, str) else None,
                entrypoint=entrypoint if isinstance(entrypoint, str) else None,
            )
        )
    return out


def rc_sessions_for(repo: Path, sessions: list[SessionInfo]) -> list[SessionInfo]:
    """Remote-control engine sessions in the repo (worktrees included) — the ones
    a supervisor restart kills.

    Interactive sessions (entrypoint "cli"/"claude-desktop") that happen to have
    their cwd inside a repo are independent processes; they neither die with the
    supervisor nor should they block its restarts.
    """
    return [
        s
        for s in sessions
        if s.entrypoint == "sdk-cli" and (s.cwd == repo or s.cwd.is_relative_to(repo))
    ]


def last_activity(session: SessionInfo) -> float:
    """Newest transcript write for this session, floored at its start time."""
    newest = session.started_at
    for transcript in claude_home().glob(f"projects/*/{session.session_id}.jsonl"):
        try:
            newest = max(newest, transcript.stat().st_mtime)
        except OSError:
            continue
    return newest


def all_quiet(sessions: list[SessionInfo], quiet_seconds: float, now: float) -> bool:
    return all(now - last_activity(s) >= quiet_seconds for s in sessions)


def repo_sessions(repo: Path) -> list[SessionInfo]:
    """The repo's remote-control engine sessions, from a fresh read."""
    return rc_sessions_for(repo, live_sessions())


def repo_quiet(repo: Path, quiet_seconds: float, now: float) -> bool:
    """Is the repo quiet — every one of its engine sessions transcript-quiet?

    Always reads fresh session state: quiet gates decide restarts and
    retirements, and a stale snapshot could mistake a just-started session
    for absence. A repo with no sessions is quiet.
    """
    return all_quiet(repo_sessions(repo), quiet_seconds, now)


def quiet_minutes(sessions: list[SessionInfo], now: float) -> float:
    """Minutes since the most recent transcript write across these sessions."""
    return min((now - last_activity(s)) / 60 for s in sessions)


def binary_version() -> str | None:
    try:
        result = subprocess.run(
            [str(claude_bin()), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search(result.stdout)
    return match.group(1) if match else None


def process_version(pid: int) -> str | None:
    """Version a running process was launched with, from its versioned binary path."""
    try:
        exe = psutil.Process(pid).exe()
    except psutil.Error:
        return None
    match = re.search(r"/versions/(\d+\.\d+\.\d+)", exe)
    return match.group(1) if match else None
