"""`groundcrew status` and `groundcrew sessions`: the fleet tables and their --json forms."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import git, make_repo, write_session

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


def test_status_table_columns_stay_aligned_past_a_long_repo_name(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    short = sandbox / "projects" / "a"
    long = sandbox / "projects" / ("b" * 60)  # far past the old fixed 34-char column
    short.mkdir(parents=True)
    long.mkdir(parents=True)
    write_state({str(short): repo_state(), str(long): repo_state()})

    assert cli.cmd_status(config.load()) == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(("a", "b"))]
    assert len(lines) == 2
    assert lines[0].index("DOWN") == lines[1].index("DOWN")


# ── `groundcrew sessions` ────────────────────────────────────────────────────


def register(repo: Path) -> None:
    write_state({str(repo): repo_state()})


def live_engine(
    repo_or_wt: Path, *, bridge_session_id: str = "session_x"
) -> subprocess.Popen[bytes]:
    """A real process standing in for a live remote-control engine, plus its session file."""
    child = subprocess.Popen(["sleep", "30"])
    write_session(
        child.pid,
        f"sess-{child.pid}",
        str(repo_or_wt),
        int(time.time() * 1000),
        entrypoint="sdk-cli",
        bridgeSessionId=bridge_session_id,
        name=f"bridge-cse-{bridge_session_id}-x1",
    )
    return child


def test_sessions_json_reports_a_worktree_anchored_session(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "demo")
    register(repo)
    wt = repo / ".claude" / "worktrees" / "bridge-cse_test"
    git(repo, "worktree", "add", "-q", "-b", "worktree-bridge-cse_test", str(wt))
    child = live_engine(wt)
    try:
        assert cli.cmd_sessions(config.load(), as_json=True) == 0
    finally:
        child.kill()
        child.wait()

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    row = rows[0]
    assert row["repo"] == str(repo)
    assert row["worktree"] == str(wt)
    assert row["address"] == "bridge-cse-session_x-x1"
    assert row["title"] is None
    assert row["pid"] == child.pid
    assert row["branch"] == "worktree-bridge-cse_test"


def test_sessions_json_reports_branch_via_current_branch_for_a_same_dir_session(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "demo", branch="main")
    register(repo)
    child = live_engine(repo)
    try:
        assert cli.cmd_sessions(config.load(), as_json=True) == 0
    finally:
        child.kill()
        child.wait()

    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["worktree"] is None
    assert rows[0]["branch"] == "main"


def test_sessions_json_is_empty_when_nothing_is_live(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "demo")
    register(repo)

    assert cli.cmd_sessions(config.load(), as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_sessions_table_reports_no_live_sessions(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "demo")
    register(repo)

    assert cli.cmd_sessions(config.load(), as_json=False) == 0
    assert capsys.readouterr().out.strip() == "no live sessions"


def test_sessions_table_shows_worktree_name_and_address(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "demo")
    register(repo)
    wt = repo / ".claude" / "worktrees" / "bridge-cse_test"
    git(repo, "worktree", "add", "-q", "-b", "worktree-bridge-cse_test", str(wt))
    child = live_engine(wt)
    try:
        assert cli.cmd_sessions(config.load(), as_json=False) == 0
    finally:
        child.kill()
        child.wait()

    out = capsys.readouterr().out
    assert "bridge-cse_test" in out
    assert "bridge-cse-session_x-x1" in out
    assert "worktree-bridge-cse_test" in out


def test_sessions_table_columns_stay_aligned_past_a_long_address(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo1 = make_repo(sandbox / "projects" / "one", branch="main")
    repo2 = make_repo(sandbox / "projects" / "two", branch="main")
    write_state({str(repo1): repo_state(), str(repo2): repo_state()})
    c1 = live_engine(repo1, bridge_session_id="a")
    c2 = live_engine(repo2, bridge_session_id="x" * 50)  # far past the old fixed 38-char column
    try:
        assert cli.cmd_sessions(config.load(), as_json=False) == 0
    finally:
        for child in (c1, c2):
            child.kill()
            child.wait()

    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith(("one", "two"))
    ]
    assert len(lines) == 2
    assert lines[0].index(" main") == lines[1].index(" main")
