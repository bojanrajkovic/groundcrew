"""Supervisor process management: spawn, adopt, liveness, termination.

Each managed repo gets one `claude remote-control` process launched with that
repo's effective settings. Children are spawned with start_new_session=True so
they survive a daemon restart (systemd KillMode=process); on startup the
daemon re-adopts them by scanning the process table (psutil) for matching
cmdline + cwd. Restarting a supervisor is safe by construction: the CLI
reconnects the same cloud environment and sessions, and preserves dirty
worktrees.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from groundcrew.claude_state import proc_create_time, process_is
from groundcrew.config import (
    BACKOFF_SECONDS,
    CRASH_LIMIT,
    CRASH_WINDOW_SECONDS,
    TERMINATE_TIMEOUT,
    RepoSettings,
    state_dir,
)


def rc_args(settings: RepoSettings) -> tuple[str, ...]:
    """The remote-control argv for a repo's effective settings.

    Defaults are emitted explicitly so a supervisor's command line is
    self-describing: args-drift detection compares a live process's argv
    against this, and an implicit default would read as drift forever.
    """
    return (
        "remote-control",
        "--spawn",
        settings.spawn,
        "--capacity",
        str(settings.capacity),
        "--permission-mode",
        settings.permission_mode,
    )


@dataclass
class Supervisor:
    repo: Path
    pid: int
    created: float
    launched_version: str | None
    launched_args: tuple[str, ...]  # argv after the binary; real cmdline for adoptees
    spawned_at: float
    handle: subprocess.Popen[bytes] | None = None  # None for adopted processes

    def alive(self) -> bool:
        if self.handle is not None:
            self.handle.poll()  # reap if it exited, so no zombies linger
        return process_is(self.pid, self.created)


def log_path(repo: Path) -> Path:
    logs = state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / (str(repo).strip("/").replace("/", "-") + ".log")


def spawn(repo: Path, version: str | None, settings: RepoSettings, binary: Path) -> Supervisor:
    args = rc_args(settings)
    # Append: a respawn must not erase the previous run's output — that is
    # exactly the crash evidence a crash-loop alert sends you to read.
    with log_path(repo).open("ab") as log:
        handle = subprocess.Popen(
            [str(binary), *args],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    created = proc_create_time(handle.pid)
    if created is None:  # died instantly; alive() will report it on the next pass
        created = -1.0
    return Supervisor(
        repo=repo,
        pid=handle.pid,
        created=created,
        launched_version=version,
        launched_args=args,
        spawned_at=time.time(),
        handle=handle,
    )


@dataclass(frozen=True)
class ProcRecord:
    """What adoption needs to know about one live process."""

    pid: int
    argv: tuple[str, ...]
    cwd: str
    created: float | None  # None: the process died between listing and inspection


def match_orphans(
    procs: Iterable[ProcRecord], root: Path, repos: list[Path]
) -> dict[Path, Supervisor]:
    """Re-adopt supervisors left running by a previous daemon instance.

    Adopts any remote-control process — regardless of spawn mode — whose cwd
    is a registered repo OR any git repo under the projects root; the latter so
    a repo unregistered across a daemon restart still gets retired instead of
    leaking forever. A hand-started remote-control in a registered repo is
    deliberately adopted too: args-drift converges it to the configured flags
    at the next quiet window.
    """
    wanted = {str(r): r for r in repos}
    adopted: dict[Path, Supervisor] = {}
    for proc in procs:
        if "remote-control" not in proc.argv or proc.created is None:
            continue
        repo = wanted.get(proc.cwd)
        if repo is None:
            candidate = Path(proc.cwd)
            if candidate.is_relative_to(root) and (candidate / ".git").exists():
                repo = candidate
        if repo is None or repo in adopted:
            continue
        adopted[repo] = Supervisor(
            repo=repo,
            pid=proc.pid,
            created=proc.created,
            launched_version=None,  # resolved later from the process exe or engine metadata
            launched_args=proc.argv[1:],  # drop the binary path
            spawned_at=time.time(),
            handle=None,
        )
    return adopted


def _proc_records() -> Iterator[ProcRecord]:
    attrs = ["pid", "cmdline", "cwd", "create_time"]
    for proc in psutil.process_iter(attrs):
        info = proc.info
        cmdline = info.get("cmdline") or []
        cwd = info.get("cwd")
        if not cmdline or not cwd:  # kernel threads, permission-denied, races
            continue
        yield ProcRecord(
            pid=info["pid"],
            argv=tuple(cmdline),
            cwd=cwd,
            created=info.get("create_time"),
        )


def find_orphans(root: Path, repos: list[Path]) -> dict[Path, Supervisor]:
    return match_orphans(_proc_records(), root, repos)


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
