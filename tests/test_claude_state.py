from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from groundcrew import claude_state
from groundcrew.config import claude_home, claude_json_path


def write_session(
    pid: int,
    session_id: str,
    cwd: str,
    started_at_ms: int,
    *,
    entrypoint: str | None = None,
) -> None:
    sessions = claude_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": started_at_ms,
        "version": "2.1.233",
    }
    if entrypoint is not None:
        data["entrypoint"] = entrypoint
    (sessions / f"{pid}.json").write_text(json.dumps(data))


def test_seed_trust_preserves_other_content(sandbox: Path) -> None:
    claude_json_path().write_text(
        json.dumps(
            {
                "userID": "abc",
                "projects": {"/existing": {"hasTrustDialogAccepted": True, "other": 1}},
            }
        )
    )
    repo = sandbox / "projects" / "demo"

    changed = claude_state.seed_trust([repo])

    assert changed == [repo]
    data = json.loads(claude_json_path().read_text())
    assert data["userID"] == "abc"
    assert data["projects"]["/existing"] == {"hasTrustDialogAccepted": True, "other": 1}
    assert data["projects"][str(repo)]["hasTrustDialogAccepted"] is True
    assert str(repo) in claude_state.trusted_paths()
    # second seed is a no-op and does not rewrite
    assert claude_state.seed_trust([repo]) == []


def test_live_sessions_skips_dead_and_recycled_pids(sandbox: Path) -> None:
    home = claude_home()
    child = subprocess.Popen(["sleep", "30"])
    try:
        now_ms = int(time.time() * 1000)
        # legit: the engine existed before its startedAt was recorded
        write_session(child.pid, "sess-live", "/repo", now_ms)
        # dead: no process wears this PID (beyond default pid_max)
        write_session(4194000, "sess-dead", "/repo", now_ms)
        # recycled: the recorded session started in 1970, long before this
        # process was created — whoever wears the PID now is a different process
        recycled = home / "sessions" / "recycled.json"
        recycled.write_text(
            json.dumps(
                {"pid": child.pid, "sessionId": "sess-recycled", "cwd": "/repo", "startedAt": 1}
            )
        )

        sessions = claude_state.live_sessions()
    finally:
        child.kill()
        child.wait()

    assert [s.session_id for s in sessions] == ["sess-live"]
    assert sessions[0].version == "2.1.233"

    # once the child is dead, its session file no longer counts as live
    assert claude_state.live_sessions() == []


def test_rc_sessions_for_filters_cwd_and_entrypoint(sandbox: Path) -> None:
    repo = Path("/home/x/proj")
    wt = repo / ".claude" / "worktrees" / "wt"
    in_worktree = claude_state.SessionInfo(1, "a", wt, 0, None, entrypoint="sdk-cli")
    at_root = claude_state.SessionInfo(2, "b", repo, 0, None, entrypoint="sdk-cli")
    outside = claude_state.SessionInfo(3, "c", Path("/home/x/other"), 0, None, entrypoint="sdk-cli")
    interactive = claude_state.SessionInfo(4, "d", repo, 0, None, entrypoint="cli")
    desktop = claude_state.SessionInfo(5, "e", repo / "sub", 0, None, entrypoint="claude-desktop")

    got = claude_state.rc_sessions_for(repo, [in_worktree, at_root, outside, interactive, desktop])

    assert [s.session_id for s in got] == ["a", "b"]


def test_quiet_detection_uses_transcript_mtime(sandbox: Path) -> None:
    home = claude_home()
    transcripts = home / "projects" / "-repo"
    transcripts.mkdir(parents=True)
    transcript = transcripts / "sess-1.jsonl"
    transcript.write_text("{}\n")
    session = claude_state.SessionInfo(1, "sess-1", Path("/repo"), started_at=0, version=None)
    now = time.time()

    os.utime(transcript, (now - 60, now - 60))  # active a minute ago
    assert not claude_state.all_quiet([session], quiet_seconds=900, now=now)

    os.utime(transcript, (now - 1000, now - 1000))  # quiet for >15 minutes
    assert claude_state.all_quiet([session], quiet_seconds=900, now=now)

    assert claude_state.all_quiet([], quiet_seconds=900, now=now)  # no sessions = quiet


def test_repo_quiet_composes_fresh_session_state(sandbox: Path) -> None:
    repo = sandbox / "projects" / "repo"
    now = time.time()
    child = subprocess.Popen(["sleep", "30"])
    try:
        write_session(
            child.pid, "sess-rc", str(repo / "sub"), int(now * 1000), entrypoint="sdk-cli"
        )

        # a session that just started is not quiet…
        assert not claude_state.repo_quiet(repo, quiet_seconds=900, now=now)
        # …but satisfies a zero-length quiet window
        assert claude_state.repo_quiet(repo, quiet_seconds=0, now=now)
        # a repo with no sessions is quiet
        assert claude_state.repo_quiet(sandbox / "projects" / "other", 900, now)
    finally:
        child.kill()
        child.wait()


def test_proc_create_time_for_own_and_missing_pid(sandbox: Path) -> None:
    created = claude_state.proc_create_time(os.getpid())
    assert created is not None
    assert created <= time.time()
    assert claude_state.proc_create_time(4194000) is None
