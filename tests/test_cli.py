"""`groundcrew status`: the fleet table and its --json form."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import write_session

from groundcrew import claude_state, cli, config
from groundcrew.daemon import FleetState
from groundcrew.supervise import RepoState


def write_state(repos: dict[str, RepoState]) -> None:
    state = FleetState(
        updated_at=time.time(),
        binary_version="2.1.233",
        pending_rollout=None,
        last_update_result="",
        registered=list(repos),
        unregistered=[],
        repos=repos,
    )
    path = config.state_dir() / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json())


def repo_state(**overrides: object) -> RepoState:
    base: dict[str, object] = {
        "pid": None,
        "created": None,
        "version": "2.1.233",
        "spawned_at": None,
        "last_pull_at": 0.0,
        "last_pull_kind": "",
        "last_pull_detail": "",
        "pull_failures": 0,
        "backoff_until": 0.0,
        "warnings": [],
    }
    base.update(overrides)
    return RepoState(**base)  # type: ignore[arg-type]


def test_status_json_reports_headline_fields_for_a_down_repo(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = sandbox / "projects" / "demo"
    repo.mkdir(parents=True)
    write_state({str(repo): repo_state()})

    assert cli.cmd_status(config.load(), as_json=True) == 0

    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "path": str(repo),
            "state": "down",
            "pid": None,
            "backoff_seconds": None,
            "version": "2.1.233",
            "session_count": 0,
            "quiet_minutes": None,
            "last_pull_kind": None,
            "last_pull_at": None,
        }
    ]


def test_status_json_reports_backoff_seconds_remaining(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = sandbox / "projects" / "demo"
    repo.mkdir(parents=True)
    write_state({str(repo): repo_state(backoff_until=time.time() + 300)})

    assert cli.cmd_status(config.load(), as_json=True) == 0

    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "backoff"
    assert rows[0]["pid"] is None
    assert 0 < rows[0]["backoff_seconds"] <= 300


def test_status_json_counts_live_sessions_for_a_running_supervisor(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = sandbox / "projects" / "demo"
    repo.mkdir(parents=True)
    child = subprocess.Popen(["sleep", "30"])
    try:
        created = claude_state.proc_create_time(child.pid)
        assert created is not None
        write_state({str(repo): repo_state(pid=child.pid, created=created)})
        write_session(
            child.pid,
            "sess-1",
            str(repo),
            int(time.time() * 1000),
            entrypoint="sdk-cli",
            bridgeSessionId="session_sess-1",
        )

        assert cli.cmd_status(config.load(), as_json=True) == 0
    finally:
        child.kill()
        child.wait()

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["path"] == str(repo)
    assert rows[0]["state"] == "up"
    assert rows[0]["pid"] == child.pid
    assert rows[0]["session_count"] == 1
    assert rows[0]["quiet_minutes"] is not None


def test_status_table_output_is_unchanged_by_the_json_refactor(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = sandbox / "projects" / "demo"
    repo.mkdir(parents=True)
    write_state({str(repo): repo_state(warnings=["disk almost full"])})

    assert cli.cmd_status(config.load()) == 0

    out = capsys.readouterr().out
    assert "REPO" in out
    assert "SESS" in out
    assert "DOWN" in out
    assert "⚠ disk almost full" in out


def test_ago_clamps_a_future_timestamp_instead_of_going_negative() -> None:
    now = time.time()
    assert cli._ago(now + 120, now) == "0m ago"
