from __future__ import annotations

import os
from pathlib import Path

import pytest

from groundcrew import cli, supervise
from groundcrew.config import RepoSettings
from groundcrew.supervise import ProcRecord, match_orphans, rc_args

RC_ARGV = (
    "/opt/claude",
    "remote-control",
    "--spawn",
    "worktree",
    "--capacity",
    "32",
    "--permission-mode",
    "bypassPermissions",
)


def rec(
    pid: int, cwd: Path, argv: tuple[str, ...] = RC_ARGV, created: float | None = 12345.0
) -> ProcRecord:
    return ProcRecord(pid=pid, argv=argv, cwd=str(cwd), created=created)


def test_rc_args_emits_defaults_explicitly() -> None:
    assert rc_args(RepoSettings()) == RC_ARGV[1:]


def test_rc_args_reflects_settings() -> None:
    settings = RepoSettings(spawn="same-dir", capacity=4, permission_mode="plan")

    args = rc_args(settings)

    assert args == (
        "remote-control",
        "--spawn",
        "same-dir",
        "--capacity",
        "4",
        "--permission-mode",
        "plan",
    )


def test_registered_repo_adopted_with_args(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    adopted = match_orphans([rec(41, repo)], tmp_path, [repo])

    assert list(adopted) == [repo]
    sup = adopted[repo]
    assert sup.pid == 41
    assert sup.launched_args == RC_ARGV[1:]
    assert sup.launched_version is None
    assert sup.handle is None


def test_unregistered_git_repo_under_root_adopted(tmp_path: Path) -> None:
    stray = tmp_path / "stray"
    (stray / ".git").mkdir(parents=True)

    adopted = match_orphans([rec(42, stray)], tmp_path, [])

    assert list(adopted) == [stray]


def test_same_dir_supervisor_adopted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = ("/opt/claude", "remote-control", "--spawn", "same-dir")

    adopted = match_orphans([rec(43, repo, argv=argv)], tmp_path, [repo])

    assert adopted[repo].launched_args == argv[1:]


def test_non_rc_process_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    adopted = match_orphans([rec(44, repo, argv=("bash", "-l"))], tmp_path, [repo])

    assert adopted == {}


def test_out_of_root_unregistered_cwd_skipped(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".git").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()

    adopted = match_orphans([rec(45, elsewhere)], root, [])

    assert adopted == {}


def test_first_process_per_repo_wins(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    adopted = match_orphans([rec(46, repo), rec(47, repo)], tmp_path, [repo])

    assert adopted[repo].pid == 46


def test_spawn_launches_the_given_binary(sandbox: Path) -> None:
    repo = sandbox / "projects" / "repo"
    repo.mkdir()
    marker = sandbox / "launched"
    fake = sandbox / "fake-claude"
    fake.write_text(f'#!/bin/sh\necho "$@" > {marker}\n')
    fake.chmod(0o755)

    sup = supervise.spawn(repo, "1.0.0", RepoSettings(), binary=fake)
    assert sup.handle is not None
    sup.handle.wait(timeout=10)

    assert "--capacity 32" in marker.read_text()


def test_proc_reader_strips_cmdline_nul_terminator() -> None:
    me = next(r for r in supervise._proc_records() if r.pid == os.getpid())

    assert me.argv
    assert me.argv[-1] != ""


def test_dead_process_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    adopted = match_orphans([rec(48, repo, created=None)], tmp_path, [repo])

    assert adopted == {}


# ── reading the supervisor log ──────────────────────────────────────────────

FRAME = (
    "\x1b[7A\x1b[J·✔︎· Connected · groundcrew\n"
    "    Capacity: 1/32 · New sessions will be created in an isolated worktree\n"
    "    \x1b]8;;https://claude.ai/code/session_01AB?from=cli\x07Attached\x1b]8;;\x07\n"
    "\n"
    "space to show QR code · w to toggle spawn mode\n"
)


def test_readable_log_strips_repaint_control_codes() -> None:
    lines = list(supervise.readable_log(FRAME))

    assert lines[0] == "·✔︎· Connected · groundcrew"
    assert "Attached" in lines[2], "the hyperlink label must survive its OSC-8 wrapper"
    assert not any("\x1b" in line for line in lines)
    assert "" not in lines


def test_readable_log_drops_repeated_frames_but_keeps_real_events() -> None:
    failure = "[19:15:36] Session failed: Process exited with error cse_01XY\n"
    log = FRAME * 20 + failure + FRAME * 20

    lines = list(supervise.readable_log(log))

    assert lines.count("space to show QR code · w to toggle spawn mode") == 1
    assert lines[-1].endswith("Session failed: Process exited with error cse_01XY")


def test_readable_log_keeps_an_event_that_recurs_after_the_frame_window() -> None:
    # Two genuine failures far apart must both survive; only redraws collapse.
    failure = "[19:15:36] Session failed: cse_01XY\n"
    log = failure + FRAME * 20 + failure

    assert list(supervise.readable_log(log)).count("[19:15:36] Session failed: cse_01XY") == 2


def test_readable_log_leaves_plain_output_alone() -> None:
    assert list(supervise.readable_log("one\ntwo\nthree\n")) == ["one", "two", "three"]


def test_cmd_logs_reads_the_repos_own_log(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = sandbox / "projects" / "thing"
    supervise.log_path(repo).write_text(FRAME * 5 + "[19:15:36] Session failed: cse_01XY\n")

    assert cli.cmd_logs(str(repo), 50, verbatim=False) == 0

    out = capsys.readouterr().out
    assert out.rstrip().endswith("[19:15:36] Session failed: cse_01XY")
    assert "\x1b" not in out
    assert out.count("space to show QR code · w to toggle spawn mode") == 1

    assert cli.cmd_logs(str(repo), 50, verbatim=True) == 0
    assert "\x1b" in capsys.readouterr().out, "--raw must hand back the bytes as written"


def test_cmd_logs_reports_a_repo_that_never_spawned(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.cmd_logs(str(sandbox / "projects" / "never"), 50, verbatim=False) == 1
    assert "no supervisor log" in capsys.readouterr().err
