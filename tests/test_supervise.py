from __future__ import annotations

import os
from pathlib import Path

from groundcrew import supervise
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
    "--create-session-in-dir",
)


def rec(
    pid: int, cwd: Path, argv: tuple[str, ...] = RC_ARGV, created: float | None = 12345.0
) -> ProcRecord:
    return ProcRecord(pid=pid, argv=argv, cwd=str(cwd), created=created)


def test_rc_args_emits_defaults_explicitly() -> None:
    assert rc_args(RepoSettings()) == RC_ARGV[1:]


def test_rc_args_reflects_settings() -> None:
    settings = RepoSettings(
        spawn="same-dir", capacity=4, permission_mode="plan", create_session_in_dir=False
    )

    args = rc_args(settings)

    assert args == (
        "remote-control",
        "--spawn",
        "same-dir",
        "--capacity",
        "4",
        "--permission-mode",
        "plan",
        "--no-create-session-in-dir",
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
