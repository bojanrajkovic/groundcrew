"""Supervisor process management: spawn, adopt, liveness, termination.

Each managed repo gets one `claude remote-control --spawn worktree` process.
Children are spawned with start_new_session=True so they survive a daemon
restart (systemd KillMode=process); on startup the daemon re-adopts them by
scanning /proc for matching cmdline + cwd. Restarting a supervisor is safe by
construction: the CLI reconnects the same cloud environment and sessions, and
preserves dirty worktrees.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from groundcrew.claude_state import proc_start
from groundcrew.config import (
    BACKOFF_SECONDS,
    CRASH_LIMIT,
    CRASH_WINDOW_SECONDS,
    TERMINATE_TIMEOUT,
    claude_bin,
    state_dir,
)

RC_ARGS = ("remote-control", "--spawn", "worktree", "--permission-mode", "bypassPermissions")


@dataclass
class Supervisor:
    repo: Path
    pid: int
    proc_start: str
    launched_version: str | None
    spawned_at: float
    handle: subprocess.Popen[bytes] | None = None  # None for adopted processes

    def alive(self) -> bool:
        if self.handle is not None:
            self.handle.poll()  # reap if it exited, so no zombies linger
        return proc_start(self.pid) == self.proc_start


def log_path(repo: Path) -> Path:
    logs = state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / (str(repo).strip("/").replace("/", "-") + ".log")


def spawn(repo: Path, version: str | None) -> Supervisor:
    # Append: a respawn must not erase the previous run's output — that is
    # exactly the crash evidence a crash-loop alert sends you to read.
    with log_path(repo).open("ab") as log:
        handle = subprocess.Popen(
            [str(claude_bin()), *RC_ARGS],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    start = proc_start(handle.pid)
    if start is None:  # died instantly; alive() will report it on the next pass
        start = "gone"
    return Supervisor(
        repo=repo,
        pid=handle.pid,
        proc_start=start,
        launched_version=version,
        spawned_at=time.time(),
        handle=handle,
    )


def find_orphans(root: Path, repos: list[Path]) -> dict[Path, Supervisor]:
    """Re-adopt supervisors left running by a previous daemon instance.

    Adopts any remote-control process whose cwd is a registered repo OR any git
    repo under the projects root — the latter so a repo unregistered across a
    daemon restart still gets retired instead of leaking forever. A hand-started
    remote-control in a registered repo is deliberately adopted too: the daemon
    converges it to the registry's declared flags at the next drift restart.
    """
    wanted = {str(r): r for r in repos}
    adopted: dict[Path, Supervisor] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            cwd = str((entry / "cwd").readlink())
        except OSError:
            continue
        if b"remote-control" not in argv or b"worktree" not in argv:
            continue
        repo = wanted.get(cwd)
        if repo is None:
            candidate = Path(cwd)
            if candidate.is_relative_to(root) and (candidate / ".git").exists():
                repo = candidate
        if repo is None or repo in adopted:
            continue
        start = proc_start(pid)
        if start is None:
            continue
        adopted[repo] = Supervisor(
            repo=repo,
            pid=pid,
            proc_start=start,
            launched_version=None,  # resolved later from /proc/<pid>/exe or engine metadata
            spawned_at=time.time(),
            handle=None,
        )
    return adopted


def terminate(sup: Supervisor, timeout: float = TERMINATE_TIMEOUT) -> bool:
    """SIGTERM, escalating to SIGKILL. Returns True once the process is gone.

    SIGTERM is the verified-clean path (environment preserved, dirty worktrees
    kept). SIGKILL as a last resort: session state lives server-side and dirty
    worktrees are only removed by the CLI's own cleanup, so a hard kill loses
    nothing durable.
    """
    with contextlib.suppress(ProcessLookupError):
        os.kill(sup.pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not sup.alive():
            return True
        time.sleep(0.5)
    with contextlib.suppress(ProcessLookupError):
        os.kill(sup.pid, signal.SIGKILL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not sup.alive():
            return True
        time.sleep(0.5)
    return False


@dataclass
class CrashTracker:
    events: deque[float] = field(default_factory=deque)
    backoff_until: float = 0.0

    def record(self, now: float) -> bool:
        """Record a death; True if this trips the crash-loop breaker."""
        self.events.append(now)
        while self.events and now - self.events[0] > CRASH_WINDOW_SECONDS:
            self.events.popleft()
        if len(self.events) >= CRASH_LIMIT:
            self.backoff_until = now + BACKOFF_SECONDS
            self.events.clear()
            return True
        return False

    def in_backoff(self, now: float) -> bool:
        return now < self.backoff_until
