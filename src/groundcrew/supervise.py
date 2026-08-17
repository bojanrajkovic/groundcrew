"""The supervised repo: entity decisions plus supervisor process management.

The entity half follows ADR 0004: SupervisedRepo methods take world
observations as values and return decisions as values; the daemon (the
imperative shell) performs effects and feeds outcomes back. The process
half below it — spawn, adopt, liveness, termination — is the mechanics the
shell uses to execute those decisions.

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
import enum
import os
import re
import signal
import subprocess
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from pydantic import BaseModel

from groundcrew.claude_state import proc_create_time, process_is
from groundcrew.config import (
    BACKOFF_SECONDS,
    CRASH_LIMIT,
    CRASH_WINDOW_SECONDS,
    PULL_FAILURES_BEFORE_ALERT,
    TERMINATE_TIMEOUT,
    RepoSettings,
    state_dir,
)
from groundcrew.gitops import PullKind, PullOutcome


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
        "--create-session-in-dir"
        if settings.create_session_in_dir
        else "--no-create-session-in-dir",
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


# `claude remote-control` draws an interactive status pane and repaints it on
# every change. On a terminal each frame overwrites the last; appended to a
# file, every repaint is kept, so the log is a stack of near-identical frames.
_CSI = r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # cursor moves and erases: the repaint itself
_OSC = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # hyperlink wrappers; the label survives
_ESCAPES = re.compile(f"{_CSI}|{_OSC}")

# One frame is a handful of lines, so a line recurring within a window this
# size is the pane redrawn rather than the same event happening twice.
FRAME_WINDOW = 40


def readable_log(text: str) -> Iterator[str]:
    """Strip the repaint control codes and drop lines the pane merely redrew.

    Recency, not adjacency: the frame is a repeating cycle of lines, so
    consecutive-duplicate collapsing would keep every copy of all of them.

    The window counts lines read, not lines kept. A frame collapses to a few
    survivors, so a window over the output would barely advance while hundreds
    of repaints scrolled past, and the second occurrence of a real event —
    a session failing the same way twice — would be swallowed as a redraw.
    """
    last_seen: dict[str, int] = {}
    for position, raw in enumerate(text.splitlines()):
        line = _ESCAPES.sub("", raw).rstrip()
        if not line:
            continue
        previous = last_seen.get(line)
        # Refresh on every sighting, including skipped ones: while the pane
        # keeps redrawing a line, it has not scrolled away and must stay quiet.
        last_seen[line] = position
        if previous is not None and position - previous <= FRAME_WINDOW:
            continue
        yield line


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


# ── the supervised repo entity (ADR 0004) ────────────────────────────────────


class WarningKind(enum.Enum):
    """Identity and lifecycle key for a repo warning.

    Each kind has exactly one producer, and that producer owns the full
    lifecycle: it clears its kinds at the top of its pass and sets them for
    conditions that hold. No function touches another producer's kinds, so
    warning correctness never depends on call ordering.
    """

    PARKED = "parked"  # freshness
    DIRTY = "dirty"
    DIVERGED = "diverged"
    PULL = "pull"
    POST_PULL = "post_pull"
    DEFERRED = "deferred"
    DRIFT = "drift"  # plan_drift
    UNTRUSTED = "untrusted"  # plan_supervision
    MISSING = "missing"


class RepoState(BaseModel):
    """One repo's row in the fleet snapshot `status` reads."""

    pid: int | None
    created: float | None
    version: str | None
    spawned_at: float | None
    last_pull_at: float
    last_pull_kind: str
    last_pull_detail: str
    pull_failures: int
    backoff_until: float
    warnings: list[str]

    def alive(self) -> bool:
        return (
            self.pid is not None and self.created is not None and process_is(self.pid, self.created)
        )


class Plan(enum.Enum):
    SPAWN = "spawn"
    WAIT = "wait"


class Retire(enum.Enum):
    TERMINATE = "terminate"
    FORGET = "forget"  # supervisor already dead; just release it
    WAIT = "wait"


class Fresh(enum.Enum):
    PULL = "pull"
    SKIP = "skip"


@dataclass(frozen=True)
class Alert:
    title: str
    message: str


@dataclass(frozen=True)
class RunHook:
    command: tuple[str, ...]


@dataclass(frozen=True)
class Restart:
    reason: str


@dataclass(frozen=True)
class Defer:
    reason: str


@dataclass
class SupervisedRepo:
    """The unit of supervision: decisions over values, bookkeeping as the only mutation.

    Methods observe the world through parameters (never by reaching out) and
    answer with decision values the shell executes. See ADR 0004.
    """

    path: Path
    settings: RepoSettings
    supervisor: Supervisor | None = None
    crashes: CrashTracker = field(default_factory=CrashTracker)
    pull_failures: int = 0
    pull_alerted: bool = False
    last_pull_at: float = 0.0
    last_pull_kind: str = ""
    last_pull_detail: str = ""
    warnings: dict[WarningKind, str] = field(default_factory=dict)

    # -- supervision ------------------------------------------------------

    def plan_supervision(
        self, now: float, *, present: bool, trusted: bool, alive: bool
    ) -> Plan | Alert:
        """Should the shell spawn a supervisor here? Alert = crash breaker tripped."""
        self.warnings.pop(WarningKind.UNTRUSTED, None)
        self.warnings.pop(WarningKind.MISSING, None)
        if not present:
            self.warnings[WarningKind.MISSING] = "missing: directory does not exist"
            return Plan.WAIT
        if self.supervisor is not None and alive:
            return Plan.WAIT
        if self.supervisor is not None:
            self.supervisor = None
            if self.crashes.record(now):
                return Alert(
                    "groundcrew: crash loop",
                    f"{self.path.name} supervisor crash-looping; backing off",
                )
        if self.crashes.in_backoff(now):
            return Plan.WAIT
        if not trusted:
            self.warnings[WarningKind.UNTRUSTED] = (
                "untrusted: run `groundcrew add` to seed workspace trust"
            )
            return Plan.WAIT
        return Plan.SPAWN

    def plan_retirement(self, *, alive: bool, quiet: bool) -> Retire:
        if self.supervisor is None:
            return Retire.WAIT
        if not alive:
            return Retire.FORGET
        return Retire.TERMINATE if quiet else Retire.WAIT

    # -- freshness --------------------------------------------------------

    def plan_freshness(self, session_count: int) -> Fresh:
        """May the shell pull? same-dir sessions share the working tree."""
        for kind in (
            WarningKind.PARKED,
            WarningKind.DIRTY,
            WarningKind.DIVERGED,
            WarningKind.PULL,
            WarningKind.POST_PULL,
            WarningKind.DEFERRED,
        ):
            self.warnings.pop(kind, None)
        if self.settings.spawn == "same-dir" and session_count > 0:
            self.warnings[WarningKind.DEFERRED] = (
                f"deferred: pull skipped, {session_count} live session(s) share the working tree"
            )
            return Fresh.SKIP
        return Fresh.PULL

    def on_pull(self, outcome: PullOutcome, now: float) -> RunHook | Alert | None:
        self.last_pull_at = now
        self.last_pull_kind = outcome.kind.value
        self.last_pull_detail = outcome.detail
        if outcome.parked:
            self.warnings[WarningKind.PARKED] = (
                "parked: checkout is not on the default branch; spawns base on HEAD"
            )
        if outcome.kind is PullKind.FETCHED_DIRTY:
            self.warnings[WarningKind.DIRTY] = f"dirty: {outcome.detail}"
        if outcome.kind is PullKind.DIVERGED:
            # A repo state needing a human; not an infrastructure failure, so it
            # neither counts toward nor masks the consecutive-failure alert.
            self.warnings[WarningKind.DIVERGED] = f"diverged: {outcome.detail}"
            return None
        if outcome.kind is PullKind.FAILED:
            self.pull_failures += 1
            self.warnings[WarningKind.PULL] = (
                f"pull failing x{self.pull_failures}: {outcome.detail}"
            )
            if self.pull_failures >= PULL_FAILURES_BEFORE_ALERT and not self.pull_alerted:
                self.pull_alerted = True
                return Alert("groundcrew: pull failing", f"{self.path.name}: {outcome.detail}")
            return None
        self.pull_failures = 0
        self.pull_alerted = False
        if outcome.moved and not outcome.parked and self.settings.post_pull:
            # Parked repos get ref-only updates; the working tree didn't change,
            # so refreshing its toolchain would act on a tree the pull never touched.
            return RunHook(self.settings.post_pull)
        return None

    def on_hook_result(self, error: str | None) -> Alert | None:
        if error is None:
            return None
        self.warnings[WarningKind.POST_PULL] = f"post_pull failed: {error}"
        return Alert("groundcrew: post_pull failed", f"{self.path.name}: {error}")

    # -- drift ------------------------------------------------------------

    def plan_drift(
        self, binary_version: str | None, probed_version: str | None, *, quiet: bool
    ) -> Restart | Defer | None:
        """Does the supervisor match the desired (version, args) pair? None = converged.

        probed_version fills a hole adoption leaves open (adoptees carry no
        launched_version); the shell observes it, the owner records it here.
        """
        self.warnings.pop(WarningKind.DRIFT, None)
        sup = self.supervisor
        if sup is None or binary_version is None:
            return None
        if sup.launched_version is None and probed_version is not None:
            sup.launched_version = probed_version
        reasons = []
        if sup.launched_version != binary_version:
            reasons.append(f"version {sup.launched_version} -> {binary_version}")
        if sup.launched_args != rc_args(self.settings):
            reasons.append("args")
        if not reasons:
            return None
        reason = " + ".join(reasons)
        if not quiet:
            self.warnings[WarningKind.DRIFT] = (
                f"drift ({reason}): restart deferred, session(s) active"
            )
            return Defer(reason)
        return Restart(reason)

    # -- snapshot ---------------------------------------------------------

    def to_state(self) -> RepoState:
        sup = self.supervisor
        return RepoState(
            pid=sup.pid if sup else None,
            created=sup.created if sup else None,
            version=sup.launched_version if sup else None,
            spawned_at=sup.spawned_at if sup else None,
            last_pull_at=self.last_pull_at,
            last_pull_kind=self.last_pull_kind,
            last_pull_detail=self.last_pull_detail,
            pull_failures=self.pull_failures,
            backoff_until=self.crashes.backoff_until,
            warnings=list(self.warnings.values()),
        )
