"""The supervision core's behavior, tested as values in / values out (ADR 0004).

No substrate, no fakes: observations go in as parameters, decisions come out
as values, and the entity's own bookkeeping is asserted directly.
"""

from __future__ import annotations

from pathlib import Path

from groundcrew.config import BACKOFF_SECONDS, RepoSettings
from groundcrew.gitops import PullKind, PullOutcome
from groundcrew.supervise import (
    Alert,
    Defer,
    Fresh,
    Plan,
    Restart,
    Retire,
    RunHook,
    SupervisedRepo,
    Supervisor,
    WarningKind,
    rc_args,
)

NOW = 1_000_000.0


def entity(settings: RepoSettings | None = None) -> SupervisedRepo:
    return SupervisedRepo(path=Path("/projects/demo"), settings=settings or RepoSettings())


def supervisor(settings: RepoSettings | None = None, version: str | None = "1.0.0") -> Supervisor:
    return Supervisor(
        repo=Path("/projects/demo"),
        pid=1234,
        created=42.0,
        launched_version=version,
        launched_args=rc_args(settings or RepoSettings()),
        spawned_at=0.0,
    )


# ── supervision planning ────────────────────────────────────────────────────


def test_missing_directory_warns_and_waits() -> None:
    repo = entity()

    assert repo.plan_supervision(NOW, present=False, trusted=True, alive=False) is Plan.WAIT
    assert WarningKind.MISSING in repo.warnings


def test_untrusted_repo_warns_and_waits() -> None:
    repo = entity()

    assert repo.plan_supervision(NOW, present=True, trusted=False, alive=False) is Plan.WAIT
    assert WarningKind.UNTRUSTED in repo.warnings


def test_trust_and_missing_warnings_clear_once_condition_lifts() -> None:
    repo = entity()
    repo.plan_supervision(NOW, present=True, trusted=False, alive=False)

    assert repo.plan_supervision(NOW, present=True, trusted=True, alive=False) is Plan.SPAWN
    assert WarningKind.UNTRUSTED not in repo.warnings


def test_live_supervisor_means_wait() -> None:
    repo = entity()
    repo.supervisor = supervisor()

    assert repo.plan_supervision(NOW, present=True, trusted=True, alive=True) is Plan.WAIT
    assert repo.supervisor is not None


def test_dead_supervisor_is_released_and_respawned_same_pass() -> None:
    repo = entity()
    repo.supervisor = supervisor()

    decision = repo.plan_supervision(NOW, present=True, trusted=True, alive=False)

    assert decision is Plan.SPAWN
    assert repo.supervisor is None


def test_third_crash_in_window_trips_breaker_with_alert() -> None:
    repo = entity()
    for i in range(2):
        repo.supervisor = supervisor()
        assert repo.plan_supervision(NOW + i, present=True, trusted=True, alive=False) is Plan.SPAWN
    repo.supervisor = supervisor()

    decision = repo.plan_supervision(NOW + 2, present=True, trusted=True, alive=False)

    assert isinstance(decision, Alert)
    assert "crash-looping" in decision.message
    # in backoff: no spawn until the window passes
    assert repo.plan_supervision(NOW + 3, present=True, trusted=True, alive=False) is Plan.WAIT
    after = NOW + 2 + BACKOFF_SECONDS + 1
    assert repo.plan_supervision(after, present=True, trusted=True, alive=False) is Plan.SPAWN


# ── retirement ──────────────────────────────────────────────────────────────


def test_retirement_decisions() -> None:
    repo = entity()
    # nothing to retire
    assert repo.plan_retirement(alive=False, quiet=True, sessions=0) is Retire.WAIT

    repo.supervisor = supervisor()
    assert repo.plan_retirement(alive=True, quiet=False, sessions=0) is Retire.WAIT
    assert repo.plan_retirement(alive=True, quiet=True, sessions=0) is Retire.TERMINATE
    assert repo.plan_retirement(alive=False, quiet=False, sessions=0) is Retire.FORGET


def test_retirement_waits_rather_than_stranding_sessions() -> None:
    """Un-registering a repo does not license losing its environment."""
    repo = entity()
    repo.supervisor = supervisor(RepoSettings(create_session_in_dir=False))

    assert repo.plan_retirement(alive=True, quiet=True, sessions=1) is Retire.WAIT
    # with an in-dir session, unaffected
    repo.supervisor = supervisor()
    assert repo.plan_retirement(alive=True, quiet=True, sessions=1) is Retire.TERMINATE


# ── freshness ───────────────────────────────────────────────────────────────


def test_sessions_confined_to_worktrees_never_block_a_pull() -> None:
    assert entity().plan_freshness(root_sessions=0) is Fresh.PULL


def test_pull_defers_while_a_session_shares_the_main_checkout() -> None:
    repo = entity(RepoSettings(spawn="same-dir"))

    assert repo.plan_freshness(root_sessions=2) is Fresh.SKIP
    assert "2 live session(s)" in repo.warnings[WarningKind.DEFERRED]
    # sessions gone → pull again, deferral warning cleared
    assert repo.plan_freshness(root_sessions=0) is Fresh.PULL
    assert WarningKind.DEFERRED not in repo.warnings


def test_in_dir_session_blocks_a_pull_in_a_worktree_repo() -> None:
    """spawn mode does not decide this: worktree repos have an in-dir session too."""
    repo = entity()  # spawn="worktree", create_session_in_dir on

    assert repo.plan_freshness(root_sessions=1) is Fresh.SKIP


def pull_outcome(
    kind: PullKind, detail: str = "", *, moved: bool = False, parked: bool = False
) -> PullOutcome:
    return PullOutcome(kind, detail, moved=moved, parked=parked)


def test_moved_pull_runs_the_configured_hook() -> None:
    repo = entity(RepoSettings(post_pull=("mise", "install")))

    decision = repo.on_pull(pull_outcome(PullKind.FF_PULLED, moved=True), NOW)

    assert decision == RunHook(("mise", "install"))
    assert repo.last_pull_kind == "ff-pulled"


def test_moved_pull_without_hook_configured_is_quiet() -> None:
    assert entity().on_pull(pull_outcome(PullKind.FF_PULLED, moved=True), NOW) is None


def test_unmoved_pull_never_runs_the_hook() -> None:
    repo = entity(RepoSettings(post_pull=("mise", "install")))

    assert repo.on_pull(pull_outcome(PullKind.FF_PULLED, moved=False), NOW) is None


def test_parked_ref_update_warns_and_skips_the_hook() -> None:
    repo = entity(RepoSettings(post_pull=("mise", "install")))

    decision = repo.on_pull(pull_outcome(PullKind.REF_UPDATED, moved=True, parked=True), NOW)

    assert decision is None
    assert WarningKind.PARKED in repo.warnings


def test_dirty_fetch_warns() -> None:
    repo = entity()
    repo.on_pull(pull_outcome(PullKind.FETCHED_DIRTY, "uncommitted work"), NOW)

    assert "uncommitted work" in repo.warnings[WarningKind.DIRTY]


def test_diverged_warns_without_counting_as_failure() -> None:
    repo = entity()
    repo.on_pull(pull_outcome(PullKind.DIVERGED, "refusing"), NOW)

    assert WarningKind.DIVERGED in repo.warnings
    assert repo.pull_failures == 0


def test_pull_failures_alert_exactly_once_at_threshold() -> None:
    repo = entity()
    failed = pull_outcome(PullKind.FAILED, "boom")

    assert repo.on_pull(failed, NOW) is None
    assert repo.on_pull(failed, NOW) is None
    third = repo.on_pull(failed, NOW)
    assert isinstance(third, Alert)
    assert "boom" in third.message
    assert repo.on_pull(failed, NOW) is None  # already alerted; no repeat
    assert "x4" in repo.warnings[WarningKind.PULL]


def test_successful_pull_resets_failure_tracking() -> None:
    repo = entity()
    failed = pull_outcome(PullKind.FAILED, "boom")
    for _ in range(3):
        repo.on_pull(failed, NOW)

    repo.on_pull(pull_outcome(PullKind.FF_PULLED), NOW)

    assert repo.pull_failures == 0
    assert WarningKind.PULL not in repo.warnings or repo.plan_freshness(0) is Fresh.PULL
    # a fresh run of failures alerts again
    for _ in range(2):
        assert repo.on_pull(failed, NOW) is None
    assert isinstance(repo.on_pull(failed, NOW), Alert)


def test_hook_failure_warns_and_alerts() -> None:
    repo = entity()

    decision = repo.on_hook_result("exit 1: kaboom")

    assert isinstance(decision, Alert)
    assert "kaboom" in decision.message
    assert "kaboom" in repo.warnings[WarningKind.POST_PULL]
    assert repo.on_hook_result(None) is None


# ── drift ───────────────────────────────────────────────────────────────────


def test_converged_supervisor_needs_nothing() -> None:
    repo = entity()
    repo.supervisor = supervisor()

    assert repo.plan_drift("1.0.0", None, quiet=True, sessions=0) is None


def test_version_drift_restarts_when_quiet() -> None:
    repo = entity()
    repo.supervisor = supervisor(version="0.9.0")

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=0)

    assert isinstance(decision, Restart)
    assert "0.9.0 -> 1.0.0" in decision.reason


def test_args_drift_restarts_when_quiet() -> None:
    repo = entity(RepoSettings(capacity=4))
    repo.supervisor = supervisor()  # args for default capacity 32

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=0)

    assert isinstance(decision, Restart)
    assert decision.reason == "args"


def test_combined_drift_names_both_reasons() -> None:
    repo = entity(RepoSettings(capacity=4))
    repo.supervisor = supervisor(version="0.9.0")

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=0)

    assert isinstance(decision, Restart)
    assert "version" in decision.reason
    assert "args" in decision.reason


def test_drift_defers_with_warning_while_busy_and_clears_when_converged() -> None:
    repo = entity()
    repo.supervisor = supervisor(version="0.9.0")

    decision = repo.plan_drift("1.0.0", None, quiet=False, sessions=0)

    assert isinstance(decision, Defer)
    assert WarningKind.DRIFT in repo.warnings
    # converged (e.g. after the shell restarted it): warning clears
    repo.supervisor = supervisor(version="1.0.0")
    assert repo.plan_drift("1.0.0", None, quiet=True, sessions=0) is None
    assert WarningKind.DRIFT not in repo.warnings


def test_probed_version_fills_the_adoption_hole() -> None:
    repo = entity()
    repo.supervisor = supervisor(version=None)  # adopted: version unknown

    assert repo.plan_drift("1.0.0", "1.0.0", quiet=True, sessions=0) is None
    assert repo.supervisor.launched_version == "1.0.0"


def test_unprobeable_version_counts_as_drift() -> None:
    repo = entity()
    repo.supervisor = supervisor(version=None)

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=0)

    assert isinstance(decision, Restart)


# Without an in-dir session a restart loses the environment, so quiet is not
# enough — see docs/restart-safety.md.


def test_repo_without_an_in_dir_session_defers_while_any_session_lives() -> None:
    settings = RepoSettings(create_session_in_dir=False)
    repo = entity(settings)
    repo.supervisor = supervisor(settings, version="0.9.0")

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=2)

    assert isinstance(decision, Defer)
    assert "would lose" in repo.warnings[WarningKind.DRIFT]


def test_repo_without_an_in_dir_session_restarts_once_sessions_end() -> None:
    settings = RepoSettings(create_session_in_dir=False)
    repo = entity(settings)
    repo.supervisor = supervisor(settings, version="0.9.0")

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=0)

    assert isinstance(decision, Restart)


def test_repo_with_an_in_dir_session_restarts_with_sessions_present() -> None:
    repo = entity()  # create_session_in_dir defaults on
    repo.supervisor = supervisor(version="0.9.0")

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=3)

    assert isinstance(decision, Restart)


def test_in_dir_session_is_read_from_the_running_supervisor_not_the_settings() -> None:
    """Config flipped on, but the live process was launched without one."""
    repo = entity()  # settings now want create_session_in_dir on
    repo.supervisor = supervisor(RepoSettings(create_session_in_dir=False))

    decision = repo.plan_drift("1.0.0", None, quiet=True, sessions=1)

    assert isinstance(decision, Defer)
    assert "would lose" in repo.warnings[WarningKind.DRIFT]


# ── warning lifecycle across passes ─────────────────────────────────────────


def test_freshness_pass_never_erases_the_drift_warning() -> None:
    repo = entity()
    repo.supervisor = supervisor(version="0.9.0")
    repo.plan_drift("1.0.0", None, quiet=False, sessions=0)
    assert WarningKind.DRIFT in repo.warnings

    repo.plan_freshness(root_sessions=0)
    repo.on_pull(pull_outcome(PullKind.FF_PULLED), NOW)

    assert WarningKind.DRIFT in repo.warnings


# ── snapshot ────────────────────────────────────────────────────────────────


def test_to_state_reflects_supervisor_and_bookkeeping() -> None:
    repo = entity()
    repo.supervisor = supervisor()
    repo.on_pull(pull_outcome(PullKind.FAILED, "boom"), NOW)

    state = repo.to_state()

    assert state.pid == 1234
    assert state.created == 42.0
    assert state.version == "1.0.0"
    assert state.last_pull_at == NOW
    assert state.pull_failures == 1
    assert any(w.startswith("pull failing") for w in state.warnings)

    repo.supervisor = None
    assert repo.to_state().pid is None
