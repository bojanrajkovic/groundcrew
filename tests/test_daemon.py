from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import add_origin_commit, clone, git, make_repo

from groundcrew import claude_state, config, gitops, supervise
from groundcrew import daemon as daemon_mod
from groundcrew.daemon import Daemon, RepoRuntime, discover_unregistered, next_nightly, notify
from groundcrew.supervise import CrashTracker


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def test_notify_passes_title_and_message_as_argv_and_env(sandbox: Path) -> None:
    out = sandbox / "out.txt"
    notifier = script(
        sandbox / "notifier", f'echo "$1|$2|$GROUNDCREW_TITLE|$GROUNDCREW_MESSAGE" > {out}'
    )

    notify((str(notifier),), "Title", "Body text")

    assert out.read_text().strip() == "Title|Body text|Title|Body text"


def test_notify_nonzero_exit_logged_not_raised(
    sandbox: Path, caplog: pytest.LogCaptureFixture
) -> None:
    notifier = script(sandbox / "notifier", "echo doom >&2\nexit 3")

    notify((str(notifier),), "T", "M")

    assert "notifier failed" in caplog.text
    assert "doom" in caplog.text


def test_notify_timeout_logged_not_raised(sandbox: Path, caplog: pytest.LogCaptureFixture) -> None:
    notifier = script(sandbox / "notifier", "sleep 5")

    notify((str(notifier),), "T", "M", timeout=0.2)

    assert "notifier failed" in caplog.text


def test_notify_unconfigured_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="groundcrew"):
        notify((), "T", "M")

    assert "suppressed" in caplog.text


def hook_fixture(sandbox: Path, hook_body: str, extra_config: str = "") -> tuple[Path, Path]:
    """An origin+clone pair with a fresh origin commit and a configured hook."""
    root = sandbox / "projects"
    origin = make_repo(root / "origin")
    repo = clone(origin, root / "repo")
    add_origin_commit(origin)
    hook = script(sandbox / "hook", hook_body)
    cfg_dir = sandbox / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.toml").write_text(f'[hooks]\npost_pull = ["{hook}"]\n{extra_config}')
    return repo, sandbox / "hook-ran"


def test_post_pull_runs_in_repo_after_branch_move(sandbox: Path) -> None:
    repo, marker = hook_fixture(sandbox, f"pwd > {sandbox / 'hook-ran'}")

    Daemon(config.load()).pull_repo(repo, RepoRuntime(), time.time())

    assert marker.read_text().strip() == str(repo)


def test_post_pull_skipped_for_parked_repo(sandbox: Path) -> None:
    repo, marker = hook_fixture(sandbox, f"pwd > {sandbox / 'hook-ran'}")
    git(repo, "checkout", "-q", "-b", "parked-branch")
    rt = RepoRuntime()

    Daemon(config.load()).pull_repo(repo, rt, time.time())

    assert not marker.exists()
    assert any(w.startswith("parked") for w in rt.warnings)


def test_post_pull_empty_override_disables(sandbox: Path) -> None:
    repo, marker = hook_fixture(sandbox, f"pwd > {sandbox / 'hook-ran'}")
    cfg_path = sandbox / "config" / "config.toml"
    cfg_path.write_text(cfg_path.read_text() + f'\n[repos."{repo}"]\npost_pull = []\n')

    Daemon(config.load()).pull_repo(repo, RepoRuntime(), time.time())

    assert not marker.exists()


def test_post_pull_failure_warns_and_notifies(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = hook_fixture(sandbox, "echo kaboom >&2\nexit 1")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        daemon_mod, "notify", lambda _cmd, title, message, **_kw: sent.append((title, message))
    )
    rt = RepoRuntime()

    Daemon(config.load()).pull_repo(repo, rt, time.time())

    assert any(w.startswith("post_pull failed") for w in rt.warnings)
    assert sent
    assert "kaboom" in sent[0][1]


def live_supervisor(repo: Path, args: tuple[str, ...], version: str) -> supervise.Supervisor:
    """A Supervisor wearing this test process's PID, so alive() is True."""
    pid = os.getpid()
    created = claude_state.proc_create_time(pid)
    assert created is not None
    return supervise.Supervisor(
        repo=repo,
        pid=pid,
        created=created,
        launched_version=version,
        launched_args=args,
        spawned_at=0.0,
    )


def rc_session(repo: Path, started_at: float) -> claude_state.SessionInfo:
    return claude_state.SessionInfo(
        pid=os.getpid(),
        session_id="test-session",
        cwd=repo / "sub",
        started_at=started_at,
        version="1.0.0",
        entrypoint="sdk-cli",
    )


def test_args_drift_restarts_quiet_supervisor(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(config.load())
    daemon.binary_version = "1.0.0"
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    rt = RepoRuntime()
    rt.supervisor = live_supervisor(repo, ("remote-control", "--old-shape"), "1.0.0")
    killed: list[supervise.Supervisor] = []

    def fake_terminate(sup: supervise.Supervisor) -> bool:
        killed.append(sup)
        return True

    monkeypatch.setattr(supervise, "terminate", fake_terminate)

    daemon.maybe_restart_for_drift(repo, rt, [])

    assert killed
    assert rt.supervisor is None


def test_matching_version_and_args_do_not_restart(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config.load()
    daemon = Daemon(cfg)
    daemon.binary_version = "1.0.0"
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    rt = RepoRuntime()
    rt.supervisor = live_supervisor(repo, supervise.rc_args(cfg.for_repo(repo)), "1.0.0")
    monkeypatch.setattr(supervise, "terminate", lambda _sup: pytest.fail("must not terminate"))

    daemon.maybe_restart_for_drift(repo, rt, [])

    assert rt.supervisor is not None


def test_drift_deferred_while_sessions_active(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(config.load())
    daemon.binary_version = "1.0.0"
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    rt = RepoRuntime()
    rt.supervisor = live_supervisor(repo, ("remote-control", "--old-shape"), "1.0.0")
    monkeypatch.setattr(claude_state, "live_sessions", lambda: [rc_session(repo, time.time())])
    monkeypatch.setattr(supervise, "terminate", lambda _sup: pytest.fail("must not terminate"))

    daemon.maybe_restart_for_drift(repo, rt, [])

    assert rt.supervisor is not None
    assert any(w.startswith("drift") for w in rt.warnings)


def test_same_dir_repo_defers_pull_while_sessions_live(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    cfg_dir = sandbox / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.toml").write_text(f'[repos."{repo}"]\nspawn = "same-dir"\n')
    daemon = Daemon(config.load())
    rt = RepoRuntime()
    monkeypatch.setattr(claude_state, "live_sessions", lambda: [rc_session(repo, time.time())])
    monkeypatch.setattr(gitops, "pull", lambda _repo: pytest.fail("pull must be skipped"))

    daemon.pull_repo(repo, rt, time.time())

    assert any(w.startswith("deferred") for w in rt.warnings)


def test_registry_round_trip(sandbox: Path) -> None:
    repos = [sandbox / "projects" / "b", sandbox / "projects" / "a"]
    config.save_registry(repos)

    loaded = config.load_registry()

    assert loaded == sorted(repos)
    # dedupe on save
    config.save_registry([*loaded, repos[0]])
    assert config.load_registry() == sorted(repos)


def test_discover_unregistered_skips_registered_and_nested(sandbox: Path) -> None:
    root = config.load().root
    managed = make_repo(root / "managed")
    make_repo(root / "unmanaged")
    make_repo(root / "group" / "unmanaged-nested-group")
    make_repo(root / "managed" / "vendored")  # nested inside a managed repo

    found = discover_unregistered([managed], root)

    names = [p.name for p in found]
    assert "unmanaged" in names
    assert "unmanaged-nested-group" in names
    assert "managed" not in names
    assert "vendored" not in names


def test_next_nightly_lands_on_the_configured_hour() -> None:
    now = time.time()
    run_at = next_nightly(now, config.NIGHTLY_HOUR)

    assert run_at > now
    assert run_at - now <= 25 * 3600  # 25h: a fall-back DST day is that long
    local = datetime.fromtimestamp(run_at).astimezone()
    assert local.hour == config.NIGHTLY_HOUR
    assert local.minute == 0


def test_next_nightly_across_dst_transitions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        # Day before spring-forward (2026-03-08) and before fall-back (2026-11-01)
        for day in ("2026-03-07", "2026-10-31"):
            after = time.mktime(time.strptime(f"{day} 12:00", "%Y-%m-%d %H:%M"))
            run_at = next_nightly(after, config.NIGHTLY_HOUR)
            assert time.localtime(run_at).tm_hour == config.NIGHTLY_HOUR
            # and the recomputation from that run must land on the NEXT day,
            # never the same day twice
            following = next_nightly(run_at, config.NIGHTLY_HOUR)
            assert time.localtime(following).tm_yday != time.localtime(run_at).tm_yday
    finally:
        monkeypatch.undo()
        time.tzset()


def test_crash_tracker_trips_after_limit_within_window() -> None:
    tracker = CrashTracker()
    now = time.time()

    assert not tracker.record(now)
    assert not tracker.record(now + 1)
    assert tracker.record(now + 2)  # third crash inside the window trips it
    assert tracker.in_backoff(now + 3)
    assert not tracker.in_backoff(now + config.BACKOFF_SECONDS + 10)


def test_crash_tracker_forgets_old_events() -> None:
    tracker = CrashTracker()
    now = time.time()

    assert not tracker.record(now)
    assert not tracker.record(now + 1)
    # third crash far outside the window does not trip the breaker
    assert not tracker.record(now + config.CRASH_WINDOW_SECONDS + 60)
