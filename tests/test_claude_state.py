from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from conftest import write_session

from groundcrew import claude_state
from groundcrew.config import claude_home, claude_json_path


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


def engine(pid: int, sid: str, cwd: Path) -> claude_state.SessionInfo:
    """A remote-control engine session: sdk-cli, and owned by a bridge session."""
    return claude_state.SessionInfo(
        pid, sid, cwd, 0, None, entrypoint="sdk-cli", bridge_session_id=f"session_{sid}"
    )


def test_rc_sessions_for_filters_cwd(sandbox: Path) -> None:
    repo = Path("/home/x/proj")
    wt = repo / ".claude" / "worktrees" / "wt"
    in_worktree = engine(1, "a", wt)
    at_root = engine(2, "b", repo)
    outside = engine(3, "c", Path("/home/x/other"))
    interactive = claude_state.SessionInfo(4, "d", repo, 0, None, entrypoint="cli")
    desktop = claude_state.SessionInfo(5, "e", repo / "sub", 0, None, entrypoint="claude-desktop")

    got = claude_state.rc_sessions_for(repo, [in_worktree, at_root, outside, interactive, desktop])

    assert [s.session_id for s in got] == ["a", "b"]


def test_rc_sessions_for_skips_headless_runs_that_are_not_the_supervisors(
    sandbox: Path,
) -> None:
    """A headless `claude -p` run reports entrypoint "sdk-cli" too.

    Counting one as a supervisor's session defers that supervisor's restarts
    behind a job it does not own. A long-running routine holds the deferral
    until the stuck-stop alert fires. Only bridge-owned sessions carry
    `bridgeSessionId`.
    """
    repo = Path("/home/x/proj")
    cron = claude_state.SessionInfo(1, "a", repo / "sub", 0, None, entrypoint="sdk-cli")

    assert claude_state.rc_sessions_for(repo, [cron, engine(2, "b", repo)]) == [
        engine(2, "b", repo)
    ]


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


# A background task produces no transcript writes while it runs, so mtime alone
# reads the session as idle. The task ids in the transcript say otherwise.

LAUNCH = '{{"content": "Command running in background (ID: {tid}). Output is..."}}'
FINISH = '{{"content": "<task-notification>\\n<task-id>{tid}</task-id>\\n<status>completed"}}'


def transcript_for(session_id: str, *lines: str) -> Path:
    transcripts = claude_home() / "projects" / "-repo"
    transcripts.mkdir(parents=True, exist_ok=True)
    path = transcripts / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_an_unused_anchor_session_has_had_no_turns(sandbox: Path) -> None:
    """`create_session_in_dir` opens a transcript and may never write to it."""
    transcripts = claude_home() / "projects" / "-repo"
    transcripts.mkdir(parents=True, exist_ok=True)
    (transcripts / "sess-anchor.jsonl").write_text("")  # opened, never used
    anchor = claude_state.SessionInfo(1, "sess-anchor", Path("/repo"), started_at=0, version=None)

    assert not claude_state.has_turns(anchor)


def test_a_session_that_has_written_a_turn_reports_one(sandbox: Path) -> None:
    transcript_for("sess-used", '{"content": "hello"}')
    used = claude_state.SessionInfo(2, "sess-used", Path("/repo"), started_at=0, version=None)

    assert claude_state.has_turns(used)


def test_a_session_with_no_transcript_at_all_has_had_no_turns(sandbox: Path) -> None:
    absent = claude_state.SessionInfo(3, "sess-none", Path("/repo"), started_at=0, version=None)

    assert not claude_state.has_turns(absent)


def test_a_session_waiting_on_a_background_task_is_never_quiet(sandbox: Path) -> None:
    path = transcript_for("sess-bg", LAUNCH.format(tid="b64gw58x8"))
    session = claude_state.SessionInfo(1, "sess-bg", Path("/repo"), started_at=0, version=None)
    now = time.time()
    os.utime(path, (now - 10_000, now - 10_000))  # silent for hours

    assert claude_state.pending_tasks(session) == {"b64gw58x8"}
    assert not claude_state.all_quiet([session], quiet_seconds=900, now=now)


def test_the_task_notification_releases_the_session(sandbox: Path) -> None:
    path = transcript_for("sess-bg", LAUNCH.format(tid="b64gw58x8"), FINISH.format(tid="b64gw58x8"))
    session = claude_state.SessionInfo(1, "sess-bg", Path("/repo"), started_at=0, version=None)
    now = time.time()
    os.utime(path, (now - 10_000, now - 10_000))

    assert claude_state.pending_tasks(session) == set()
    assert claude_state.all_quiet([session], quiet_seconds=900, now=now)


def test_only_the_unfinished_task_holds_the_session(sandbox: Path) -> None:
    path = transcript_for(
        "sess-bg",
        LAUNCH.format(tid="done1"),
        FINISH.format(tid="done1"),
        LAUNCH.format(tid="still2"),
    )
    session = claude_state.SessionInfo(1, "sess-bg", Path("/repo"), started_at=0, version=None)
    now = time.time()
    os.utime(path, (now - 10_000, now - 10_000))

    assert claude_state.pending_tasks(session) == {"still2"}
    assert not claude_state.all_quiet([session], quiet_seconds=900, now=now)


def test_repo_quiet_composes_fresh_session_state(sandbox: Path) -> None:
    repo = sandbox / "projects" / "repo"
    child = subprocess.Popen(["sleep", "30"])
    try:
        # startedAt must postdate the child's creation, like a real engine's
        # does — otherwise live_sessions() reads the child as a PID recycler.
        now = time.time()
        write_session(
            child.pid,
            "sess-rc",
            str(repo / "sub"),
            int(now * 1000),
            entrypoint="sdk-cli",
            bridgeSessionId="session_rc",
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


def test_repo_quiet_ignores_a_headless_run_in_the_repo(sandbox: Path) -> None:
    """A `claude -p` routine writes a real metadata file with no bridge id.

    The census must skip it. Otherwise a cron job running in a repo blocks that
    repo's restarts.
    """
    repo = sandbox / "projects" / "repo"
    child = subprocess.Popen(["sleep", "30"])
    try:
        now = time.time()
        write_session(
            child.pid, "sess-cron", str(repo / "sub"), int(now * 1000), entrypoint="sdk-cli"
        )

        assert claude_state.repo_sessions(repo) == []
        assert claude_state.repo_quiet(repo, quiet_seconds=900, now=now)
    finally:
        child.kill()
        child.wait()


def test_binary_version_probes_the_given_binary(sandbox: Path) -> None:
    fake = sandbox / "fake-claude"
    fake.write_text('#!/bin/sh\necho "9.9.9 (Claude Code)"\n')
    fake.chmod(0o755)

    assert claude_state.binary_version(fake) == "9.9.9"
    assert claude_state.binary_version(sandbox / "missing") is None


def test_proc_create_time_for_own_and_missing_pid(sandbox: Path) -> None:
    created = claude_state.proc_create_time(os.getpid())
    assert created is not None
    assert created <= time.time()
    assert claude_state.proc_create_time(4194000) is None
