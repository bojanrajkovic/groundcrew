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
  busy/idle is inferred from transcript mtime (quiet-for-N-minutes heuristic),
  corrected by the transcript's own record of unfinished background tasks.
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

from groundcrew.config import atomic_write, claude_home, claude_json_path

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")

# A backgrounded tool run is announced with its task id and later reported
# finished under the same id. Both markers are literal in the JSONL, so the
# transcript is matched as text rather than parsed.
_TASK_LAUNCHED = re.compile(r"background \(ID: ([A-Za-z0-9]+)\)")
_TASK_FINISHED = re.compile(r"<task-id>([A-Za-z0-9]+)</task-id>")


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


def process_is(pid: int, created: float) -> bool:
    """Is the process wearing this PID the one recorded at `created`?

    The one implementation of the PID-reuse-safe identity check: a recycled
    PID has a different creation time, a dead one has none.
    """
    return proc_create_time(pid) == created


@dataclass(frozen=True)
class SessionInfo:
    pid: int
    session_id: str
    cwd: Path
    started_at: float  # unix seconds
    version: str | None
    entrypoint: str | None = None  # "sdk-cli" for remote-control engines
    bridge_session_id: str | None = None  # "session_…" only for bridge-owned engines


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
        bridge = data.get("bridgeSessionId")
        out.append(
            SessionInfo(
                pid=pid,
                session_id=session_id,
                cwd=Path(cwd),
                started_at=started_ms / 1000 if isinstance(started_ms, (int, float)) else 0.0,
                version=version if isinstance(version, str) else None,
                entrypoint=entrypoint if isinstance(entrypoint, str) else None,
                bridge_session_id=bridge if isinstance(bridge, str) else None,
            )
        )
    return out


def rc_sessions_for(repo: Path, sessions: list[SessionInfo]) -> list[SessionInfo]:
    """The repo's engine sessions, worktrees included: the ones a restart kills.

    Only bridge-owned engines carry `bridgeSessionId`, so it identifies them.
    `entrypoint` does not, because a headless `claude -p` run reports "sdk-cli"
    like an engine. Counting such a run defers the supervisor's restarts behind
    a job it does not own, until the stuck-stop alert fires. Interactive
    sessions ("cli" and "claude-desktop") survive a restart, so they do not
    count either.
    """
    return [
        s
        for s in sessions
        if s.bridge_session_id is not None and (s.cwd == repo or s.cwd.is_relative_to(repo))
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


def pending_tasks(session: SessionInfo) -> set[str]:
    """Task ids this session backgrounded and has not been told finished.

    Scanning the whole transcript is affordable because callers only reach here
    once mtime already says the session looks idle, which is hourly at most.
    """
    launched: set[str] = set()
    finished: set[str] = set()
    for transcript in claude_home().glob(f"projects/*/{session.session_id}.jsonl"):
        try:
            text = transcript.read_text(errors="replace")
        except OSError:
            continue
        launched.update(_TASK_LAUNCHED.findall(text))
        finished.update(_TASK_FINISHED.findall(text))
    return launched - finished


def has_turns(session: SessionInfo) -> bool:
    """Has this session ever taken a turn, or is it an unused placeholder?

    `create_session_in_dir` pre-creates an anchor session to hold the
    environment across a restart. It opens a transcript and, unless somebody
    works in the repo root, never writes to it. Such a session has no turn for a
    restart to interrupt, so counting it as one cries wolf.
    """
    for transcript in claude_home().glob(f"projects/*/{session.session_id}.jsonl"):
        try:
            if transcript.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def session_quiet(session: SessionInfo, quiet_seconds: float, now: float) -> bool:
    """Is this session idle, rather than merely silent?

    Transcript mtime answers "silent". It cannot answer "idle": an engine
    waiting on a backgrounded tool run writes nothing for as long as the wait
    lasts, so a build or a CI watch reads exactly like a finished turn. An
    unfinished task id is the standing evidence that the wait is still on.
    """
    if now - last_activity(session) < quiet_seconds:
        return False
    return not pending_tasks(session)


def all_quiet(sessions: list[SessionInfo], quiet_seconds: float, now: float) -> bool:
    return all(session_quiet(s, quiet_seconds, now) for s in sessions)


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


def binary_version(binary: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
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
