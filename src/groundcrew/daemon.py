"""The daemon: the imperative shell around the supervised repo entities.

Loop shape:
- every POLL_SECONDS: reconcile the fleet against the registry (spawn missing,
  respawn dead with crash-loop backoff, retire unregistered when quiet), then
  persist a state snapshot for `groundcrew status`.
- every TICK_SECONDS: per repo — freshness pull, the post-pull hook when the
  default branch moved in-tree, and a drift restart once every session in the
  repo has been transcript-quiet for QUIET_SECONDS.
- nightly at NIGHTLY_HOUR: `claude update` as a backstop for the auto-updater.

Per ADR 0004 the shell performs effects and observations; every decision
belongs to SupervisedRepo, which consumes observations as values and answers
with decision values this module executes. Fleet-wide policy stays here: the
spawn ramp throttles the execution of Spawn decisions, and the nightly update
and rollout tracking span repos.

The daemon exits without touching its children (systemd KillMode=process); a
new instance re-adopts them from the process table.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import FrameType

from pydantic import BaseModel

from groundcrew import claude_state, gitops, supervise
from groundcrew.config import (
    MAX_SPAWNS_PER_PASS,
    NOTIFY_TIMEOUT,
    POLL_SECONDS,
    UPDATE_TIMEOUT,
    Config,
    atomic_write,
    load_registry,
    state_dir,
)
from groundcrew.supervise import (
    Alert,
    Fresh,
    Plan,
    RepoState,
    Restart,
    Retire,
    RunHook,
    SupervisedRepo,
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


class FleetState(BaseModel):
    """The snapshot written every poll pass and rendered by `groundcrew status`."""

    updated_at: float
    binary_version: str | None
    pending_rollout: str | None
    last_update_result: str
    registered: list[str]
    unregistered: list[str]
    repos: dict[str, RepoState]


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.fleet: dict[Path, SupervisedRepo] = {}
        self.stop = threading.Event()
        self.binary_version: str | None = None
        self.pending_rollout: str | None = None
        self.last_update_result = ""
        self.unregistered: list[str] = []

    def repo(self, path: Path) -> SupervisedRepo:
        if path not in self.fleet:
            self.fleet[path] = SupervisedRepo(path=path, settings=self.cfg.for_repo(path))
        return self.fleet[path]

    def alert(self, a: Alert) -> None:
        notify(self.cfg.notify_command, a.title, a.message)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        registry = load_registry()
        for path, sup in supervise.find_orphans(self.cfg.root, registry).items():
            self.repo(path).supervisor = sup
            log.info("adopted supervisor pid=%d for %s", sup.pid, path)
        self.binary_version = claude_state.binary_version(self.cfg.claude_bin)
        log.info(
            "groundcrew up: %d repos registered, %d supervisors adopted, claude %s",
            len(registry),
            sum(1 for sr in self.fleet.values() if sr.supervisor),
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
        running = sum(1 for sr in self.fleet.values() if sr.supervisor)
        log.info("stopping; leaving %d supervisors running for re-adoption", running)
        self.write_state(load_registry())

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %d", signum)
        self.stop.set()

    # -- supervision -------------------------------------------------------

    def reconcile(self, registry: list[Path], now: float) -> None:
        trusted = claude_state.trusted_paths()
        spawned_this_pass = 0
        for path in registry:
            sr = self.repo(path)
            alive = sr.supervisor is not None and sr.supervisor.alive()
            if sr.supervisor is not None and not alive:
                log.warning("supervisor for %s died (pid=%d)", path, sr.supervisor.pid)
            decision = sr.plan_supervision(
                now, present=path.is_dir(), trusted=str(path) in trusted, alive=alive
            )
            if isinstance(decision, Alert):
                log.error("crash loop for %s; backing off", path)
                self.alert(decision)
                continue
            if decision is not Plan.SPAWN:
                continue
            if spawned_this_pass >= MAX_SPAWNS_PER_PASS:
                continue  # ramp: the rest spawn on the next pass, 30s from now
            try:
                sr.supervisor = supervise.spawn(
                    path, self.binary_version, sr.settings, self.cfg.claude_bin
                )
            except OSError:
                log.exception("could not spawn supervisor for %s", path)
                continue
            spawned_this_pass += 1
            log.info("spawned supervisor pid=%d for %s", sr.supervisor.pid, path)
        self.retire_unregistered(registry, now)

    def retire_unregistered(self, registry: list[Path], now: float) -> None:
        # Entities are kept (supervisor=None) rather than deleted so a repo that
        # is removed and re-added keeps its crash-tracker history.
        for path, sr in self.fleet.items():
            if path in registry or sr.supervisor is None:
                continue
            alive = sr.supervisor.alive()
            quiet = alive and claude_state.repo_quiet(path, self.cfg.quiet_seconds, now)
            decision = sr.plan_retirement(alive=alive, quiet=quiet)
            if decision is Retire.FORGET:
                sr.supervisor = None
            elif decision is Retire.TERMINATE:
                log.info("retiring supervisor for unregistered %s", path)
                supervise.terminate(sr.supervisor)
                sr.supervisor = None

    # -- hourly tick -------------------------------------------------------

    def tick(self, registry: list[Path], now: float) -> None:
        self.refresh_binary_version()
        sessions = claude_state.live_sessions()
        for path in registry:
            sr = self.repo(path)
            if not path.is_dir():
                continue
            try:
                self.freshen(path, sr, now)
            except Exception:
                log.exception("pull failed unexpectedly for %s", path)
            try:
                self.converge(path, sr, sessions)
            except Exception:
                log.exception("drift check failed for %s", path)
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

    def freshen(self, path: Path, sr: SupervisedRepo, now: float) -> None:
        # Only same-dir entities decide on session presence; skip the read otherwise.
        count = len(claude_state.repo_sessions(path)) if sr.settings.spawn == "same-dir" else 0
        if sr.plan_freshness(session_count=count) is Fresh.SKIP:
            return
        outcome = gitops.pull(path)
        decision = sr.on_pull(outcome, now)
        if isinstance(decision, Alert):
            log.warning("pull failing for %s: %s", path, outcome.detail)
            self.alert(decision)
            return
        if isinstance(decision, RunHook):
            log.info("%s: %s (default branch moved)", path.name, outcome.kind.value)
            error = self.run_hook(path, decision.command)
            hook_alert = sr.on_hook_result(error)
            if hook_alert is not None:
                log.warning("post_pull failed for %s: %s", path, error)
                self.alert(hook_alert)

    def run_hook(self, path: Path, command: tuple[str, ...]) -> str | None:
        """Execute a post-pull hook; the error summary is the observation fed back."""
        try:
            result = subprocess.run(
                command,
                cwd=path,
                capture_output=True,
                text=True,
                timeout=self.cfg.post_pull_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc)
        if result.returncode == 0:
            return None
        return gitops.summarize(result.stderr) or "non-zero exit"

    def converge(
        self, path: Path, sr: SupervisedRepo, sessions: list[claude_state.SessionInfo]
    ) -> None:
        sup = sr.supervisor
        if sup is None or not sup.alive():
            return
        probed = None
        if sup.launched_version is None:
            probed = claude_state.process_version(sup.pid) or next(
                (s.version for s in claude_state.rc_sessions_for(path, sessions) if s.version),
                None,
            )
        quiet = claude_state.repo_quiet(path, self.cfg.quiet_seconds, time.time())
        decision = sr.plan_drift(self.binary_version, probed, quiet=quiet)
        if decision is None:
            return
        if isinstance(decision, Restart):
            log.info("stopping %s for drift (%s); ramp respawns it", path.name, decision.reason)
            if not supervise.terminate(sup):
                log.error("could not stop supervisor pid=%d for %s", sup.pid, path)
                return
            # Respawn happens via reconcile's spawn ramp so a fleet-wide update
            # cannot stampede the registration rate limit.
            sr.supervisor = None
        else:
            log.info("%s drifted (%s) but session(s) active; deferring", path.name, decision.reason)

    def check_rollout_complete(self) -> None:
        if self.pending_rollout is None or self.pending_rollout != self.binary_version:
            return
        fleet = [sr.supervisor for sr in self.fleet.values() if sr.supervisor is not None]
        if fleet and all(s.launched_version == self.pending_rollout for s in fleet):
            self.alert(
                Alert(
                    "groundcrew: fleet updated",
                    f"all {len(fleet)} supervisors now on claude {self.pending_rollout}",
                )
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
            self.alert(Alert("groundcrew: claude update failed", str(exc)))
            return
        tail = (result.stdout + result.stderr).strip()[-300:]
        self.last_update_result = f"exit {result.returncode}: {tail}"
        if result.returncode != 0:
            self.alert(Alert("groundcrew: claude update failed", tail))
        self.refresh_binary_version()

    # -- state snapshot ----------------------------------------------------

    def write_state(self, registry: list[Path]) -> None:
        repos = {
            str(path): sr.to_state()
            for path, sr in self.fleet.items()
            # fully retired entities keep their crash history but leave status
            if path in registry or sr.supervisor is not None
        }
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
