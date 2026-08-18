from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import (
    add_origin_commit,
    clone,
    git,
    make_repo,
    script,
    write_config,
    write_session,
)

from groundcrew import claude_state, cli, config, supervise
from groundcrew.config import RepoSettings
from groundcrew.daemon import (
    Daemon,
    JournalPriority,
    discover_unregistered,
    log_handler,
    next_nightly,
    notify,
)
from groundcrew.supervise import CrashTracker, WarningKind

# ── the notifier contract ───────────────────────────────────────────────────


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


# ── shell end-to-end: freshness through real git, hooks, and notifier ───────


def test_freshen_runs_hook_in_repo_after_branch_move(sandbox: Path) -> None:
    root = sandbox / "projects"
    origin = make_repo(root / "origin")
    repo = clone(origin, root / "repo")
    add_origin_commit(origin)
    marker = sandbox / "hook-ran"
    hook = script(sandbox / "hook", f"pwd > {marker}")
    write_config(sandbox, f'[hooks]\npost_pull = ["{hook}"]\n')
    daemon = Daemon(config.load())

    daemon.freshen(repo, daemon.repo(repo), time.time())

    assert marker.read_text().strip() == str(repo)


def test_freshen_skips_the_pull_while_a_session_sits_in_the_main_checkout(
    sandbox: Path,
) -> None:
    """A worktree repo still has an in-dir session, and a pull would move it."""
    root = sandbox / "projects"
    origin = make_repo(root / "origin")
    repo = clone(origin, root / "repo")
    add_origin_commit(origin)
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    daemon = Daemon(config.load())  # spawn defaults to worktree

    engine = subprocess.Popen(["sleep", "30"])
    try:
        write_session(
            engine.pid,
            "sess-indir",
            str(repo),
            int(time.time() * 1000),
            entrypoint="sdk-cli",
        )
        daemon.freshen(repo, daemon.repo(repo), time.time())
    finally:
        engine.kill()
        engine.wait()

    assert git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert WarningKind.DEFERRED in daemon.repo(repo).warnings


def test_freshen_hook_failure_reaches_the_real_notifier(sandbox: Path) -> None:
    root = sandbox / "projects"
    origin = make_repo(root / "origin")
    repo = clone(origin, root / "repo")
    add_origin_commit(origin)
    hook = script(sandbox / "hook", "echo kaboom >&2\nexit 1")
    sent = sandbox / "sent.txt"
    notifier = script(sandbox / "notifier", f'echo "$1|$2" > {sent}')
    write_config(
        sandbox,
        f'[notify]\ncommand = ["{notifier}"]\n\n[hooks]\npost_pull = ["{hook}"]\n',
    )
    daemon = Daemon(config.load())
    sr = daemon.repo(repo)

    daemon.freshen(repo, sr, time.time())

    assert "post_pull failed" in sent.read_text()
    assert "kaboom" in sent.read_text()
    assert WarningKind.POST_PULL in sr.warnings


# ── shell end-to-end: the spawn ramp with real processes ────────────────────


def test_reconcile_ramps_spawns_across_passes(sandbox: Path) -> None:
    fake_claude = script(sandbox / "fake-claude", "exec sleep 30")
    write_config(sandbox, f'[claude]\nbin = "{fake_claude}"\n')
    registry = []
    for i in range(5):
        repo = sandbox / "projects" / f"repo{i}"
        repo.mkdir()
        registry.append(repo)
    (sandbox / "claude.json").write_text("{}")
    claude_state.seed_trust(registry)
    daemon = Daemon(config.load())
    try:
        daemon.reconcile(registry, time.time())
        up_after_first = sum(1 for sr in daemon.fleet.values() if sr.supervisor)

        daemon.reconcile(registry, time.time())
        up_after_second = sum(1 for sr in daemon.fleet.values() if sr.supervisor)
    finally:
        for sr in daemon.fleet.values():
            if sr.supervisor and sr.supervisor.handle:
                sr.supervisor.handle.kill()
                sr.supervisor.handle.wait()

    assert up_after_first == config.MAX_SPAWNS_PER_PASS
    assert up_after_second == 5


def test_reconcile_skips_untrusted_repos(sandbox: Path) -> None:
    repo = sandbox / "projects" / "untrusted"
    repo.mkdir()
    (sandbox / "claude.json").write_text("{}")
    daemon = Daemon(config.load())

    daemon.reconcile([repo], time.time())

    sr = daemon.fleet[repo]
    assert sr.supervisor is None
    assert WarningKind.UNTRUSTED in sr.warnings


# ── shell end-to-end: drift restart terminates a real supervisor ────────────


def test_converge_restarts_a_real_drifted_supervisor(sandbox: Path) -> None:
    fake_claude = script(sandbox / "fake-claude", "exec sleep 30")
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    daemon = Daemon(config.load())
    sr = daemon.repo(repo)
    # launched with default args; entity wants capacity 4 → args drift
    sr.settings = RepoSettings(capacity=4)
    sr.supervisor = supervise.spawn(repo, "1.0.0", RepoSettings(), binary=fake_claude)
    handle = sr.supervisor.handle
    assert handle is not None
    daemon.binary_version = "1.0.0"

    daemon.converge(repo, sr, sessions=[])

    # read through the fleet, not the narrowed local, so mypy doesn't
    # consider the assertion statically false
    assert daemon.fleet[repo].supervisor is None
    assert handle.poll() is not None  # the real process is gone


def test_converge_spares_a_drifted_supervisor_that_would_lose_its_sessions(
    sandbox: Path,
) -> None:
    """Without an in-dir session, a quiet session dies with the environment."""
    write_config(sandbox, "[timing]\nquiet_seconds = 0\n")  # quiet is satisfied
    fake_claude = script(sandbox / "fake-claude", "exec sleep 30")
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    settings = RepoSettings(create_session_in_dir=False)
    daemon = Daemon(config.load())
    sr = daemon.repo(repo)
    sr.settings = settings
    sr.supervisor = supervise.spawn(repo, "0.9.0", settings, binary=fake_claude)
    handle = sr.supervisor.handle
    assert handle is not None
    daemon.binary_version = "1.0.0"  # version drift

    engine = subprocess.Popen(["sleep", "30"])
    try:
        write_session(
            engine.pid, "sess-rc", str(repo / "wt"), int(time.time() * 1000), entrypoint="sdk-cli"
        )
        daemon.converge(repo, sr, sessions=[])
    finally:
        engine.kill()
        engine.wait()

    assert daemon.fleet[repo].supervisor is not None
    assert handle.poll() is None  # still running: the environment was not lost
    assert "would lose" in sr.warnings[WarningKind.DRIFT]
    handle.terminate()


# ── snapshot round trip ─────────────────────────────────────────────────────


def test_state_round_trips_from_daemon_to_status(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = config.load()
    daemon = Daemon(cfg)
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    sr = daemon.repo(repo)
    pid = os.getpid()
    created = claude_state.proc_create_time(pid)
    assert created is not None
    sr.supervisor = supervise.Supervisor(
        repo=repo,
        pid=pid,
        created=created,
        launched_version="9.9.9",
        launched_args=("remote-control",),
        spawned_at=0.0,
    )
    sr.warnings[WarningKind.PARKED] = "parked: test warning"

    daemon.write_state([repo])

    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert f"up {pid}" in out  # liveness via the shared process_is rule
    assert "9.9.9" in out
    assert "⚠ parked: test warning" in out


# ── registry, discovery, scheduling, crash tracking ─────────────────────────


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


# ── log formatting ──────────────────────────────────────────────────────────


def record(level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord("groundcrew", level, __file__, 1, message, None, None)


def test_journal_formatter_maps_levels_to_syslog_priorities() -> None:
    fmt = JournalPriority("%(message)s")

    assert fmt.format(record(logging.INFO, "up")) == "<6>up"
    assert fmt.format(record(logging.WARNING, "pull failing")) == "<4>pull failing"
    assert fmt.format(record(logging.ERROR, "crash loop")) == "<3>crash loop"


def test_journal_formatter_prefixes_every_line_of_a_traceback() -> None:
    try:
        int("not a registry")
    except ValueError:
        exc_info = sys.exc_info()
    rec = logging.LogRecord("groundcrew", logging.ERROR, __file__, 1, "boom", None, exc_info)

    lines = JournalPriority("%(message)s").format(rec).splitlines()

    assert len(lines) > 1, "expected the traceback to be part of the formatted record"
    assert all(line.startswith("<3>") for line in lines)


def test_log_handler_uses_journal_priorities_only_under_systemd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    assert log_handler().format(record(logging.WARNING, "pull failing")) == "<4>pull failing"

    # launchd and a bare terminal get a flat line instead: no journald fields
    # are there to carry the level and the timestamp.
    monkeypatch.delenv("JOURNAL_STREAM")
    plain = log_handler().format(record(logging.WARNING, "pull failing"))
    assert plain.endswith("WARNING: pull failing")
    assert not plain.startswith("<")
