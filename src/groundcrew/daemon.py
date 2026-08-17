"""The daemon: supervise the fleet, tick hourly, update nightly.

Loop shape:
- every POLL_SECONDS: reconcile supervisors against the registry (spawn missing,
  respawn dead with crash-loop backoff, retire unregistered when quiet), then
  persist a state snapshot for `groundcrew status`.
- every TICK_SECONDS: per repo — freshness pull, the post-pull hook when the
  default branch moved in-tree, and a drift restart once every session in the
  repo has been transcript-quiet for QUIET_SECONDS.
- nightly at NIGHTLY_HOUR: `claude update` as a backstop for the auto-updater.

The daemon exits without touching its children (systemd KillMode=process); a new
instance re-adopts them from the process table.
"""

from __future__ import annotations

import enum
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

from pydantic import BaseModel

from groundcrew import claude_state, gitops, supervise
from groundcrew.config import (
    MAX_SPAWNS_PER_PASS,
    NOTIFY_TIMEOUT,
    POLL_SECONDS,
    PULL_FAILURES_BEFORE_ALERT,
    UPDATE_TIMEOUT,
    Config,
    atomic_write,
    load_registry,
    state_dir,
)

log = logging.getLogger("groundcrew")


def notify(
    command: tuple[str, ...], title: str, message: str, timeout: float = NOTIFY_TIMEOUT
) -> None:
    """Run the configured notifier command; log-and-continue on any failure.

    The contract (ADR 0001): title and message arrive both as the two appended
    argv entries and as GROUNDCREW_TITLE / GROUNDCREW_MESSAGE in the
    environment, so one-line shell notifiers stay one line and future fields
    can be added without breaking anyone. Never retried, never fatal.
    """
    if not command:
        log.info("no notifier configured; suppressed: %s — %s", title, message)
        return
    try:
        subprocess.run(
            [*command, title, message],
            env={**os.environ, "GROUNDCREW_TITLE": title, "GROUNDCREW_MESSAGE": message},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        stderr = getattr(exc, "stderr", None)
        detail = stderr.decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        log.warning("notifier failed: %s · %s", exc, gitops.summarize(detail))


def next_nightly(after: float, hour: int) -> float:
    """Unix time of the next `hour` o'clock, local time, strictly after `after`.

    mktime with tm_isdst=-1 resolves each candidate date's own UTC offset, so
    DST transitions neither shift the hour nor double-run the update.
    """
    lt = time.localtime(after)
    for days_ahead in (0, 1):
        candidate = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday + days_ahead, hour, 0, 0, 0, 0, -1)
        )
        if candidate > after:
            return candidate
    raise AssertionError("no nightly slot within two days")


def discover_unregistered(registry: list[Path], root: Path) -> list[Path]:
    """Git repos at depth 1-2 under the projects root that nobody registered."""
    registered = set(registry)
    found: list[Path] = []
    candidates = [p for p in root.glob("*") if p.is_dir()] + [
        p for p in root.glob("*/*") if p.is_dir()
    ]
    for path in sorted(candidates):
        if not (path / ".git").exists() or path in registered:
            continue
        if any(parent in registered for parent in path.parents):
            continue  # nested inside a managed repo (e.g. vendored clones)
        found.append(path)
    return found


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
            self.pid is not None
            and self.created is not None
            and claude_state.process_is(self.pid, self.created)
        )


class FleetState(BaseModel):
    """The snapshot written every poll pass and rendered by `groundcrew status`."""

    updated_at: float
    binary_version: str | None
    pending_rollout: str | None
    last_update_result: str
    registered: list[str]
    unregistered: list[str]
    repos: dict[str, RepoState]


class WarningKind(enum.Enum):
    """Identity and lifecycle key for a repo warning.

    Each kind has exactly one producer, and that producer owns the full
    lifecycle: it clears its kinds at the top of its pass and sets them for
    conditions that hold. No function touches another producer's kinds, so
    warning correctness never depends on call ordering.
    """

    PARKED = "parked"  # pull_repo
    DIRTY = "dirty"
    DIVERGED = "diverged"
    PULL = "pull"
    POST_PULL = "post_pull"
    DEFERRED = "deferred"
    DRIFT = "drift"  # maybe_restart_for_drift
    UNTRUSTED = "untrusted"  # reconcile
    MISSING = "missing"


@dataclass
class RepoRuntime:
    supervisor: supervise.Supervisor | None = None
    crashes: supervise.CrashTracker = field(default_factory=supervise.CrashTracker)
    pull_failures: int = 0
    pull_alerted: bool = False
    last_pull_at: float = 0.0
    last_pull_kind: str = ""
    last_pull_detail: str = ""
    warnings: dict[WarningKind, str] = field(default_factory=dict)


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.runtimes: dict[Path, RepoRuntime] = {}
        self.stop = threading.Event()
        self.binary_version: str | None = None
        self.pending_rollout: str | None = None
        self.last_update_result = ""
        self.unregistered: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        registry = load_registry()
        for repo, sup in supervise.find_orphans(self.cfg.root, registry).items():
            self.runtimes.setdefault(repo, RepoRuntime()).supervisor = sup
            log.info("adopted supervisor pid=%d for %s", sup.pid, repo)
        self.binary_version = claude_state.binary_version(self.cfg.claude_bin)
        log.info(
            "groundcrew up: %d repos registered, %d supervisors adopted, claude %s",
            len(registry),
            sum(1 for r in self.runtimes.values() if r.supervisor),
            self.binary_version,
        )
        tick_due = 0.0  # first tick immediately
        nightly_due = next_nightly(time.time(), self.cfg.nightly_hour)
        while not self.stop.is_set():
            # One repo's (or one file's) bad day must never take the daemon down:
            # a crash here means Restart=on-failure wipes crash trackers,
            # failure counters, and launched versions every 10 seconds.
            try:
                now = time.time()
                try:
                    registry = load_registry()
                except Exception:
                    log.exception("registry unreadable; keeping previous registry")
                self.reconcile(registry, now)
                if now >= tick_due:
                    self.tick(registry, now)
                    tick_due = now + self.cfg.tick_seconds
                if now >= nightly_due:
                    self.nightly()
                    nightly_due = next_nightly(time.time(), self.cfg.nightly_hour)
                self.write_state(registry)
            except Exception:
                log.exception("daemon loop iteration failed; continuing")
            self.stop.wait(POLL_SECONDS)
        running = sum(1 for r in self.runtimes.values() if r.supervisor)
        log.info("stopping; leaving %d supervisors running for re-adoption", running)
        self.write_state(load_registry())

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d", signum)
        self.stop.set()

    # -- supervision -------------------------------------------------------

    def reconcile(self, registry: list[Path], now: float) -> None:
        trusted = claude_state.trusted_paths()
        spawned_this_pass = 0
        for repo in registry:
            rt = self.runtimes.setdefault(repo, RepoRuntime())
            rt.warnings.pop(WarningKind.UNTRUSTED, None)
            rt.warnings.pop(WarningKind.MISSING, None)
            if not repo.is_dir():
                rt.warnings[WarningKind.MISSING] = "missing: directory does not exist"
                continue
            if rt.supervisor is not None and rt.supervisor.alive():
                continue
            if rt.supervisor is not None:
                log.warning("supervisor for %s died (pid=%d)", repo, rt.supervisor.pid)
                rt.supervisor = None
                if rt.crashes.record(now):
                    log.error("crash loop for %s; backing off", repo)
                    notify(
                        self.cfg.notify_command,
                        "groundcrew: crash loop",
                        f"{repo.name} supervisor crash-looping; backing off",
                    )
            if rt.crashes.in_backoff(now):
                continue
            if str(repo) not in trusted:
                rt.warnings[WarningKind.UNTRUSTED] = (
                    "untrusted: run `groundcrew add` to seed workspace trust"
                )
                continue
            if spawned_this_pass >= MAX_SPAWNS_PER_PASS:
                continue  # ramp: the rest spawn on the next pass, 30s from now
            try:
                rt.supervisor = supervise.spawn(
                    repo, self.binary_version, self.cfg.for_repo(repo), self.cfg.claude_bin
                )
            except OSError:
                log.exception("could not spawn supervisor for %s", repo)
                continue
            spawned_this_pass += 1
            log.info("spawned supervisor pid=%d for %s", rt.supervisor.pid, repo)
        self.retire_unregistered(registry, now)

    def retire_unregistered(self, registry: list[Path], now: float) -> None:
        # Entries are kept (supervisor=None) rather than deleted so a repo that
        # is removed and re-added keeps its crash-tracker history.
        for repo, rt in self.runtimes.items():
            if repo in registry:
                continue
            sup = rt.supervisor
            if sup is None:
                continue
            if not sup.alive():
                rt.supervisor = None
                continue
            if claude_state.repo_quiet(repo, self.cfg.quiet_seconds, now):
                log.info("retiring supervisor for unregistered %s", repo)
                supervise.terminate(sup)
                rt.supervisor = None

    # -- hourly tick -------------------------------------------------------

    def tick(self, registry: list[Path], now: float) -> None:
        self.refresh_binary_version()
        sessions = claude_state.live_sessions()
        for repo in registry:
            rt = self.runtimes.setdefault(repo, RepoRuntime())
            if not repo.is_dir():
                continue
            try:
                self.pull_repo(repo, rt, now)
            except Exception:
                log.exception("pull failed unexpectedly for %s", repo)
            try:
                self.maybe_restart_for_drift(repo, rt, sessions)
            except Exception:
                log.exception("drift check failed for %s", repo)
        self.check_rollout_complete()
        # Discovery is a filesystem sweep; hourly freshness is plenty for a
        # status hint, so it lives here rather than on every 30 s state write.
        self.unregistered = [str(p) for p in discover_unregistered(registry, self.cfg.root)]

    def refresh_binary_version(self) -> None:
        version = claude_state.binary_version(self.cfg.claude_bin)
        if version and version != self.binary_version:
            log.info("claude binary now %s (was %s)", version, self.binary_version)
            if self.binary_version is not None:
                self.pending_rollout = version
            self.binary_version = version

    def pull_repo(self, repo: Path, rt: RepoRuntime, now: float) -> None:
        for kind in (
            WarningKind.PARKED,
            WarningKind.DIRTY,
            WarningKind.DIVERGED,
            WarningKind.PULL,
            WarningKind.POST_PULL,
            WarningKind.DEFERRED,
        ):
            rt.warnings.pop(kind, None)
        if self.cfg.for_repo(repo).spawn == "same-dir":
            live = claude_state.repo_sessions(repo)
            if live:
                # All same-dir sessions share the repo's working tree, so a
                # pull would race their edits; quiet is not enough here.
                rt.warnings[WarningKind.DEFERRED] = (
                    f"deferred: pull skipped, {len(live)} live session(s) share the working tree"
                )
                return
        outcome = gitops.pull(repo)
        rt.last_pull_at = now
        rt.last_pull_kind = outcome.kind.value
        rt.last_pull_detail = outcome.detail
        if outcome.parked:
            rt.warnings[WarningKind.PARKED] = (
                "parked: checkout is not on the default branch; spawns base on HEAD"
            )
        if outcome.kind is gitops.PullKind.FETCHED_DIRTY:
            rt.warnings[WarningKind.DIRTY] = f"dirty: {outcome.detail}"
        if outcome.kind is gitops.PullKind.DIVERGED:
            # A repo state needing a human; not an infrastructure failure, so it
            # neither counts toward nor masks the consecutive-failure alert.
            rt.warnings[WarningKind.DIVERGED] = f"diverged: {outcome.detail}"
            return
        if outcome.kind is gitops.PullKind.FAILED:
            rt.pull_failures += 1
            rt.warnings[WarningKind.PULL] = f"pull failing x{rt.pull_failures}: {outcome.detail}"
            log.warning(
                "pull failed for %s (%d consecutive): %s", repo, rt.pull_failures, outcome.detail
            )
            if rt.pull_failures >= PULL_FAILURES_BEFORE_ALERT and not rt.pull_alerted:
                rt.pull_alerted = True
                notify(
                    self.cfg.notify_command,
                    "groundcrew: pull failing",
                    f"{repo.name}: {outcome.detail}",
                )
            return
        rt.pull_failures = 0
        rt.pull_alerted = False
        if outcome.moved and not outcome.parked:
            # Parked repos get ref-only updates; the working tree didn't change,
            # so refreshing its toolchain would act on a tree the pull never touched.
            log.info("%s: %s (default branch moved)", repo.name, outcome.kind.value)
            self.run_post_pull(repo, rt)

    def run_post_pull(self, repo: Path, rt: RepoRuntime) -> None:
        command = self.cfg.for_repo(repo).post_pull
        if not command:
            return
        try:
            result = subprocess.run(
                command,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=self.cfg.post_pull_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = None
            detail = str(exc)
        if result is not None and result.returncode == 0:
            return
        detail = detail if result is None else (gitops.summarize(result.stderr) or "non-zero exit")
        rt.warnings[WarningKind.POST_PULL] = f"post_pull failed: {detail}"
        log.warning("post_pull failed for %s: %s", repo, detail)
        notify(self.cfg.notify_command, "groundcrew: post_pull failed", f"{repo.name}: {detail}")

    def maybe_restart_for_drift(
        self,
        repo: Path,
        rt: RepoRuntime,
        sessions: list[claude_state.SessionInfo],
    ) -> None:
        rt.warnings.pop(WarningKind.DRIFT, None)
        sup = rt.supervisor
        if sup is None or self.binary_version is None or not sup.alive():
            return
        if sup.launched_version is None:
            sup.launched_version = claude_state.process_version(sup.pid) or next(
                (s.version for s in claude_state.rc_sessions_for(repo, sessions) if s.version),
                None,
            )
        reasons = []
        if sup.launched_version != self.binary_version:
            reasons.append(f"version {sup.launched_version} -> {self.binary_version}")
        if sup.launched_args != supervise.rc_args(self.cfg.for_repo(repo)):
            reasons.append("args")
        if not reasons:
            return
        reason = " + ".join(reasons)
        if not claude_state.repo_quiet(repo, self.cfg.quiet_seconds, time.time()):
            rt.warnings[WarningKind.DRIFT] = (
                f"drift ({reason}): restart deferred, session(s) active"
            )
            log.info("%s drifted (%s) but session(s) active; deferring", repo.name, reason)
            return
        log.info("stopping %s for drift (%s); ramp respawns it", repo.name, reason)
        if not supervise.terminate(sup):
            log.error("could not stop supervisor pid=%d for %s", sup.pid, repo)
            return
        # Respawn happens via reconcile's spawn ramp so a fleet-wide update
        # cannot stampede the registration rate limit.
        rt.supervisor = None

    def check_rollout_complete(self) -> None:
        if self.pending_rollout is None or self.pending_rollout != self.binary_version:
            return
        fleet = [rt.supervisor for rt in self.runtimes.values() if rt.supervisor is not None]
        if fleet and all(s.launched_version == self.pending_rollout for s in fleet):
            notify(
                self.cfg.notify_command,
                "groundcrew: fleet updated",
                f"all {len(fleet)} supervisors now on claude {self.pending_rollout}",
            )
            self.pending_rollout = None

    # -- nightly -----------------------------------------------------------

    def nightly(self) -> None:
        log.info("nightly claude update")
        try:
            result = subprocess.run(
                [str(self.cfg.claude_bin), "update"],
                capture_output=True,
                text=True,
                timeout=UPDATE_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_update_result = f"failed: {exc}"
            notify(self.cfg.notify_command, "groundcrew: claude update failed", str(exc))
            return
        tail = (result.stdout + result.stderr).strip()[-300:]
        self.last_update_result = f"exit {result.returncode}: {tail}"
        if result.returncode != 0:
            notify(self.cfg.notify_command, "groundcrew: claude update failed", tail)
        self.refresh_binary_version()

    # -- state snapshot ----------------------------------------------------

    def write_state(self, registry: list[Path]) -> None:
        repos: dict[str, RepoState] = {}
        for repo, rt in self.runtimes.items():
            if repo not in registry and rt.supervisor is None:
                continue  # fully retired; keep runtime for crash history, not for status
            sup = rt.supervisor
            repos[str(repo)] = RepoState(
                pid=sup.pid if sup else None,
                created=sup.created if sup else None,
                version=sup.launched_version if sup else None,
                spawned_at=sup.spawned_at if sup else None,
                last_pull_at=rt.last_pull_at,
                last_pull_kind=rt.last_pull_kind,
                last_pull_detail=rt.last_pull_detail,
                pull_failures=rt.pull_failures,
                backoff_until=rt.crashes.backoff_until,
                warnings=list(rt.warnings.values()),
            )
        state = FleetState(
            updated_at=time.time(),
            binary_version=self.binary_version,
            pending_rollout=self.pending_rollout,
            last_update_result=self.last_update_result,
            registered=[str(r) for r in registry],
            unregistered=self.unregistered,
            repos=repos,
        )
        atomic_write(state_dir() / "state.json", state.model_dump_json(indent=2))


def run_daemon(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    Daemon(cfg).run()
