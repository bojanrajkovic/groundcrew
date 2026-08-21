"""`groundcrew add`: what it registers, what it infers, and what it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_repo, write_config, write_registry

from groundcrew import cli, config
from groundcrew.config import _RegistryEntry

HEADER = (
    "# Directories managed by groundcrew, and their per-directory settings.\n"
    "# Written by `groundcrew add` / `groundcrew remove`; comments are not preserved.\n"
)


def run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    """Drive the CLI end to end, so the flags parse the way a user's do."""
    monkeypatch.setattr("sys.argv", ["groundcrew", *argv])
    return cli.main()


def scratch_dir(sandbox: Path, name: str = "scratch") -> Path:
    """A directory git does not manage."""
    path = sandbox / "projects" / name
    path.mkdir()
    return path


# ── inference: a directory git does not manage ──────────────────────────────


def test_a_non_git_directory_gets_same_dir_written_into_its_entry(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = scratch_dir(sandbox)

    assert cli.cmd_add(config.load(), [str(scratch)], {}) == 0
    assert config.load_registry() == [_RegistryEntry(path=scratch, spawn="same-dir")]
    assert 'not a git repository, so spawn = "same-dir"' in capsys.readouterr().out


def test_spawn_worktree_on_a_non_git_directory_refuses(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = scratch_dir(sandbox)

    assert cli.cmd_add(config.load(), [str(scratch)], {"spawn": "worktree"}) == 1
    out = capsys.readouterr().out
    assert f"refused {scratch}" in out
    assert 'spawn = "worktree" needs one' in out
    assert not config.load_registry()


def test_an_entry_that_already_says_worktree_is_refused_not_rewritten(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Somebody chose worktree for this directory. Say no rather than editing the choice."""
    scratch = scratch_dir(sandbox)
    write_registry(f'[[repos]]\npath = "{scratch}"\nspawn = "worktree"\n')
    before = config.registry_path().read_text()

    assert cli.cmd_add(config.load(), [str(scratch)], {}) == 1
    assert f"refused {scratch}" in capsys.readouterr().out
    assert config.registry_path().read_text() == before


def test_a_non_git_directory_whose_entry_says_same_dir_needs_no_inference(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = scratch_dir(sandbox)
    write_registry(f'[[repos]]\npath = "{scratch}"\nspawn = "same-dir"\n')

    assert cli.cmd_add(config.load(), [str(scratch)], {}) == 0
    out = capsys.readouterr().out
    assert "freshness pulls are skipped" in out
    assert "so spawn" not in out
    # re-registering keeps the entry that let it in
    assert config.load_registry() == [_RegistryEntry(path=scratch, spawn="same-dir")]


def test_a_non_git_directory_under_a_same_dir_default_needs_no_inference(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(sandbox, '[claude]\nspawn = "same-dir"\n')
    scratch = scratch_dir(sandbox)

    assert cli.cmd_add(config.load(), [str(scratch)], {}) == 0
    out = capsys.readouterr().out
    assert "freshness pulls are skipped" in out
    assert "so spawn" not in out
    # the global default already covers it; nothing gets stamped into the entry
    assert config.load_registry() == [_RegistryEntry(path=scratch)]


def test_a_repository_without_a_remote_registers_with_a_note(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "repo")

    assert cli.cmd_add(config.load(), [str(repo)], {}) == 0
    assert "no git remote; pulls will be skipped" in capsys.readouterr().out
    assert config.load_registry() == [_RegistryEntry(path=repo)]


# ── add or update ───────────────────────────────────────────────────────────


def test_a_bare_add_writes_an_entry_with_no_settings(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(sandbox / "projects" / "repo")

    assert run(monkeypatch, "add", str(repo)) == 0
    assert config.registry_path().read_text() == HEADER + f'\n[[repos]]\npath = "{repo}"\n'


def test_flags_change_only_the_settings_they_name(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "repo")
    write_registry(f'[[repos]]\npath = "{repo}"\ncapacity = 8\npost_pull = ["mise", "install"]\n')

    assert cli.cmd_add(config.load(), [str(repo)], {"permission_mode": "plan"}) == 0
    assert config.load_registry() == [
        _RegistryEntry(path=repo, capacity=8, permission_mode="plan", post_pull=["mise", "install"])
    ]
    assert "settings updated" in capsys.readouterr().out


def test_no_flags_on_a_registered_path_leaves_the_file_alone(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "repo")
    cfg = config.load()
    assert cli.cmd_add(cfg, [str(repo)], {"capacity": 8}) == 0
    before = config.registry_path().read_text()
    capsys.readouterr()

    assert cli.cmd_add(cfg, [str(repo)], {}) == 0
    assert config.registry_path().read_text() == before
    assert "already registered" in capsys.readouterr().out


def test_a_directory_that_is_not_one_is_skipped(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = sandbox / "projects" / "gone"

    assert cli.cmd_add(config.load(), [str(missing)], {}) == 1
    assert "not a directory" in capsys.readouterr().out
    assert not config.load_registry()


# ── the flags themselves ────────────────────────────────────────────────────


def test_every_flag_round_trips(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(sandbox / "projects" / "repo")

    assert (
        run(
            monkeypatch,
            "add",
            "--spawn",
            "same-dir",
            "--capacity",
            "8",
            "--permission-mode",
            "plan",
            "--no-create-session-in-dir",
            "--post-pull",
            "mise install",
            str(repo),
        )
        == 0
    )
    assert config.load_registry() == [
        _RegistryEntry(
            path=repo,
            spawn="same-dir",
            capacity=8,
            permission_mode="plan",
            create_session_in_dir=False,
            post_pull=["mise", "install"],
        )
    ]


def test_post_pull_splits_like_a_shell(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(sandbox / "projects" / "repo")

    assert run(monkeypatch, "add", "--post-pull", 'sh -c "a && b"', str(repo)) == 0
    assert config.load_registry() == [_RegistryEntry(path=repo, post_pull=["sh", "-c", "a && b"])]


def test_no_post_pull_disables_the_hook_for_the_repo(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(sandbox, '[hooks]\npost_pull = ["mise", "install"]\n')
    repo = make_repo(sandbox / "projects" / "repo")

    assert run(monkeypatch, "add", "--no-post-pull", str(repo)) == 0
    (registered,) = config.load_registry()
    assert registered == _RegistryEntry(path=repo, post_pull=[])
    assert config.effective(config.load().defaults, registered).post_pull == ()


def test_post_pull_and_no_post_pull_together_are_rejected(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(sandbox / "projects" / "repo")

    with pytest.raises(SystemExit):
        run(monkeypatch, "add", "--post-pull", "x", "--no-post-pull", str(repo))
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--spawn", "--permission-mode"])
def test_a_value_outside_the_literal_is_rejected(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    repo = make_repo(sandbox / "projects" / "repo")

    with pytest.raises(SystemExit):
        run(monkeypatch, "add", flag, "bogus", str(repo))
    assert "invalid choice" in capsys.readouterr().err


def test_an_unbalanced_quote_in_post_pull_is_a_message_not_a_traceback(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo in a flag is a usage error, so argparse owns it — not the config path."""
    repo = make_repo(sandbox / "projects" / "repo")

    with pytest.raises(SystemExit):
        run(monkeypatch, "add", "--post-pull", 'sh -c "oops', str(repo))
    assert "--post-pull" in capsys.readouterr().err
    assert not config.load_registry()
