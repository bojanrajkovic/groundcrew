from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import make_repo

from groundcrew import config
from groundcrew.daemon import discover_unregistered, next_nightly
from groundcrew.supervise import CrashTracker


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
