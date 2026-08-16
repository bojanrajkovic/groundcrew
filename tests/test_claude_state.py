from __future__ import annotations

import json
import os
import time
from pathlib import Path

from groundcrew import claude_state
from groundcrew.config import claude_home, claude_json_path


def write_session(home: Path, pid: int, session_id: str, cwd: str, proc_start: str | None) -> None:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": int(time.time() * 1000),
        "version": "2.1.233",
    }
    if proc_start is not None:
        data["procStart"] = proc_start
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


def test_live_sessions_skips_dead_and_reused_pids(sandbox: Path) -> None:
    home = claude_home()
    own_pid = os.getpid()
    own_start = claude_state.proc_start(own_pid)
    assert own_start is not None
    write_session(home, own_pid, "sess-live", "/repo", own_start)
    write_session(home, 4194000, "sess-dead", "/repo", "1")  # beyond default pid_max
    other = home / "sessions" / "reused.json"
    other.write_text(
        json.dumps(
            {
                "pid": own_pid,
                "sessionId": "sess-reused",
                "cwd": "/repo",
                "startedAt": 0,
                "procStart": "not-our-start",
            }
        )
    )

    sessions = claude_state.live_sessions()

    assert [s.session_id for s in sessions] == ["sess-live"]
    assert sessions[0].version == "2.1.233"


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


def test_proc_start_for_own_and_missing_pid(sandbox: Path) -> None:
    assert claude_state.proc_start(os.getpid()) is not None
    assert claude_state.proc_start(4194000) is None
