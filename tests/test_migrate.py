from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from conftest import write_config, write_registry

from groundcrew import cli, config, migrate
from groundcrew.config import _RegistryEntry


def config_toml(sandbox: Path, body: str) -> Path:
    """Write config.toml at column 0 — indentation is what the scanner keys on."""
    write_config(sandbox, dedent(body).lstrip("\n"))
    return sandbox / "config" / "config.toml"


def register(sandbox: Path, *names: str) -> list[Path]:
    paths = [sandbox / "projects" / name for name in names]
    for path in paths:
        path.mkdir()
    write_registry("".join(f'[[repos]]\npath = "{p}"\n\n' for p in paths))
    return paths


# ── the whole trip ──────────────────────────────────────────────────────────


def test_a_real_config_migrates_end_to_end(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (working,) = register(sandbox, "Working")
    path = config_toml(
        sandbox,
        f"""
        # how the fleet spawns sessions
        [claude]
        capacity = 8

        # the monorepo checkout, too big to duplicate per worktree
        [repos."{working}"]
        spawn = "same-dir"
        create_session_in_dir = false

        [timing]
        nightly_hour = 2
        """,
    )
    original = path.read_text()

    migrate.migrate_config()

    assert config.load_registry() == [
        _RegistryEntry(path=working, spawn="same-dir", create_session_in_dir=False)
    ]
    text = path.read_text()
    assert "[repos." not in text
    assert "too big to duplicate" not in text  # the table's own comment went with it
    assert "# how the fleet spawns sessions" in text  # [claude]'s did not
    assert "capacity = 8" in text
    assert "nightly_hour = 2" in text
    assert (sandbox / "config" / "config.toml.bak").read_text() == original
    assert config.load().defaults.capacity == 8

    out = capsys.readouterr().out
    assert "migrated per-repo settings from config.toml to repos.toml" in out
    assert "config.toml was rewritten; the original is config.toml.bak" in out
    assert f"{working} — spawn, create_session_in_dir" in out


def test_main_migrates_before_the_config_loads(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The un-migrated file no longer parses, so nothing may load it first."""
    (working,) = register(sandbox, "Working")
    path = config_toml(sandbox, f'[repos."{working}"]\ncapacity = 4\n')
    monkeypatch.setattr("sys.argv", ["groundcrew", "remove", "/not/registered"])

    assert cli.main() == 0
    assert "[repos." not in path.read_text()
    assert config.load_registry() == [_RegistryEntry(path=working, capacity=4)]


# ── which lines the surgery takes ───────────────────────────────────────────


def test_adjacent_tables_both_go(sandbox: Path) -> None:
    a, b = register(sandbox, "a", "b")
    path = config_toml(
        sandbox,
        f"""
        [claude]
        capacity = 8

        [repos."{a}"]
        capacity = 1
        [repos."{b}"]
        capacity = 2
        """,
    )

    migrate.migrate_config()

    assert path.read_text() == "[claude]\ncapacity = 8\n"
    assert config.load_registry() == [
        _RegistryEntry(path=a, capacity=1),
        _RegistryEntry(path=b, capacity=2),
    ]


def test_a_table_first_in_the_file_and_one_last_at_eof(sandbox: Path) -> None:
    a, b = register(sandbox, "a", "b")
    path = config_toml(
        sandbox,
        f"""
        [repos."{a}"]
        capacity = 1

        [claude]
        capacity = 8

        [repos."{b}"]
        capacity = 2
        """,
    )

    migrate.migrate_config()

    assert path.read_text() == "[claude]\ncapacity = 8\n"


def test_a_multi_line_array_inside_a_table(sandbox: Path) -> None:
    (a,) = register(sandbox, "a")
    path = config_toml(
        sandbox,
        f"""
        [repos."{a}"]
        post_pull = [
          "mise",
          "install",
        ]

        [timing]
        nightly_hour = 2
        """,
    )

    migrate.migrate_config()

    assert path.read_text() == "[timing]\nnightly_hour = 2\n"
    assert config.load_registry() == [_RegistryEntry(path=a, post_pull=["mise", "install"])]


def test_a_nested_array_is_not_read_as_a_table_header() -> None:
    """No registry field takes one, so this is the scanner's own postcondition."""
    text = '[claude]\ncapacity = 8\n\n[repos."/a"]\npost_pull = [\n  ["mise"],\n]\n\n[timing]\n'

    # the sections the table sat between keep their separator
    assert migrate._strip_repo_tables(text) == "[claude]\ncapacity = 8\n\n[timing]\n"


def test_removal_leaves_the_surrounding_sections_readable() -> None:
    """config.toml is the user's file, so it has to come out looking hand-kept."""
    middle = '[claude]\ncapacity = 8\n\n[repos."/a"]\ncapacity = 1\n\n[timing]\n'
    adjacent = '[claude]\ncapacity = 8\n\n[repos."/a"]\nx = 1\n\n[repos."/b"]\ny = 2\n\n[timing]\n'
    at_eof = '[claude]\ncapacity = 8\n\n[repos."/a"]\ncapacity = 1\n'
    at_start = '[repos."/a"]\ncapacity = 1\n\n[timing]\n'

    assert migrate._strip_repo_tables(middle) == "[claude]\ncapacity = 8\n\n[timing]\n"
    assert migrate._strip_repo_tables(adjacent) == "[claude]\ncapacity = 8\n\n[timing]\n"
    assert migrate._strip_repo_tables(at_eof) == "[claude]\ncapacity = 8\n"
    assert migrate._strip_repo_tables(at_start) == "[timing]\n"


def test_a_comment_block_above_the_table_goes_with_it(sandbox: Path) -> None:
    (a,) = register(sandbox, "a")
    path = config_toml(
        sandbox,
        f"""
        [claude]
        capacity = 8

        # two lines
        # about this repo
        [repos."{a}"]
        capacity = 1
        """,
    )

    migrate.migrate_config()

    assert path.read_text() == "[claude]\ncapacity = 8\n"


def test_a_comment_attached_to_the_setting_above_stays(sandbox: Path) -> None:
    """No blank line above it, so it documents `capacity`, not the table."""
    (a,) = register(sandbox, "a")
    path = config_toml(
        sandbox,
        f"""
        [claude]
        capacity = 8
        # eight is plenty
        [repos."{a}"]
        capacity = 1
        """,
    )

    migrate.migrate_config()

    assert path.read_text() == "[claude]\ncapacity = 8\n# eight is plenty\n"


# ── aborts: every one of these leaves both files alone ──────────────────────


def assert_nothing_written(sandbox: Path, config_text: str, registry_text: str) -> None:
    assert (sandbox / "config" / "config.toml").read_text() == config_text
    assert config.registry_path().read_text() == registry_text
    assert not (sandbox / "config" / "config.toml.bak").exists()


def test_an_inline_repos_table_aborts(sandbox: Path) -> None:
    (a,) = register(sandbox, "a")
    path = config_toml(sandbox, f'repos = {{ "{a}" = {{ capacity = 1 }} }}\n')
    before = path.read_text(), config.registry_path().read_text()

    with pytest.raises(config.ConfigError, match="repos"):
        migrate.migrate_config()

    assert_nothing_written(sandbox, *before)


def test_an_indented_header_aborts(sandbox: Path) -> None:
    (a,) = register(sandbox, "a")
    path = config_toml(
        sandbox,
        f"""
        [claude]
        capacity = 8

          [repos."{a}"]
          capacity = 1
        """,
    )
    before = path.read_text(), config.registry_path().read_text()

    with pytest.raises(config.ConfigError, match="repos"):
        migrate.migrate_config()

    assert_nothing_written(sandbox, *before)


def test_a_table_that_is_not_a_valid_entry_aborts(sandbox: Path) -> None:
    a, b = register(sandbox, "a", "b")
    path = config_toml(
        sandbox,
        f"""
        [repos."{a}"]
        capacity = 1

        [repos."{b}"]
        spawn = "session"
        """,
    )
    before = path.read_text(), config.registry_path().read_text()

    with pytest.raises(config.ConfigError, match="session"):
        migrate.migrate_config()

    assert_nothing_written(sandbox, *before)  # including the table that was fine


# ── the quiet paths ─────────────────────────────────────────────────────────


def test_a_table_for_an_unregistered_directory_is_dropped_and_named(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (a,) = register(sandbox, "a")
    path = config_toml(
        sandbox,
        f"""
        [repos."{a}"]
        capacity = 1

        [repos."/never/registered"]
        capacity = 2
        permission_mode = "plan"
        """,
    )

    migrate.migrate_config()

    assert path.read_text() == ""
    assert config.load_registry() == [_RegistryEntry(path=a, capacity=1)]
    out = capsys.readouterr().out
    assert "dropped per-repo settings for directories that are not registered" in out
    assert '/never/registered — capacity = 2, permission_mode = "plan"' in out


def test_a_second_run_changes_nothing(sandbox: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (a,) = register(sandbox, "a")
    path = config_toml(sandbox, f'[claude]\ncapacity = 8\n\n[repos."{a}"]\ncapacity = 1\n')
    migrate.migrate_config()
    capsys.readouterr()
    after = path.read_text(), config.registry_path().read_text()

    migrate.migrate_config()

    assert capsys.readouterr().out == ""
    assert (path.read_text(), config.registry_path().read_text()) == after


def test_no_config_file_at_all(sandbox: Path, capsys: pytest.CaptureFixture[str]) -> None:
    migrate.migrate_config()

    assert capsys.readouterr().out == ""
    assert not (sandbox / "config" / "config.toml.bak").exists()


def test_a_config_without_repo_tables(sandbox: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = config_toml(sandbox, "[claude]\ncapacity = 8\n")

    migrate.migrate_config()

    assert capsys.readouterr().out == ""
    assert path.read_text() == "[claude]\ncapacity = 8\n"
    assert not (sandbox / "config" / "config.toml.bak").exists()


def test_two_spellings_of_one_directory_abort(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging them would drop one table's settings and report both as migrated."""
    monkeypatch.setenv("HOME", str(sandbox))
    (working,) = register(sandbox, "Working")
    path = config_toml(
        sandbox,
        f"""
        [repos."{working}"]
        capacity = 1

        [repos."~/projects/Working"]
        capacity = 2
        """,
    )
    before = path.read_text(), config.registry_path().read_text()

    with pytest.raises(config.ConfigError, match="one directory"):
        migrate.migrate_config()

    assert_nothing_written(sandbox, *before)


def test_paths_match_through_a_tilde_and_a_symlink(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this migration exists for: [repos."~/Sync/x"] must find /real/x."""
    monkeypatch.setenv("HOME", str(sandbox))
    (working,) = register(sandbox, "Working")
    (sandbox / "Sync").symlink_to(sandbox / "projects")
    config_toml(sandbox, '[repos."~/Sync/Working"]\ncapacity = 4\n')

    migrate.migrate_config()

    assert config.load_registry() == [_RegistryEntry(path=working, capacity=4)]


def test_a_comment_block_documenting_the_next_section_stays() -> None:
    """A blank line separates it from this table's settings, so it belongs below."""
    text = (
        '[claude]\ncapacity = 4\n\n[repos."/a"]\ncapacity = 8\n\n'
        "# Timing controls how often we poll. Do not lower below 60.\n"
        "[timing]\nquiet_seconds = 60\n"
    )

    assert migrate._strip_repo_tables(text) == (
        "[claude]\ncapacity = 4\n\n"
        "# Timing controls how often we poll. Do not lower below 60.\n"
        "[timing]\nquiet_seconds = 60\n"
    )


def test_a_comment_touching_the_tables_last_setting_goes_with_the_table() -> None:
    """Nothing separates it from `capacity = 8`, so it documents that, not [timing]."""
    text = (
        '[claude]\ncapacity = 4\n\n[repos."/a"]\ncapacity = 8\n'
        "# eight because the monorepo is slow\n\n[timing]\nquiet_seconds = 60\n"
    )

    assert migrate._strip_repo_tables(text) == (
        "[claude]\ncapacity = 4\n\n[timing]\nquiet_seconds = 60\n"
    )


def test_a_comment_block_at_eof_is_the_last_tables_trailer() -> None:
    """No header follows, so there is nothing below for it to document."""
    text = '[claude]\ncapacity = 4\n\n[repos."/a"]\ncapacity = 8\n\n# and that is that\n'

    assert migrate._strip_repo_tables(text) == "[claude]\ncapacity = 4\n"


def test_a_drop_only_migration_still_names_the_backup(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """config.toml was rewritten without being asked; say where the original went."""
    path = config_toml(sandbox, '[repos."/never/registered"]\ncapacity = 2\n')

    migrate.migrate_config()

    assert path.read_text() == ""
    out = capsys.readouterr().out
    assert "config.toml.bak" in out
    assert (sandbox / "config" / "config.toml.bak").read_text() != ""
