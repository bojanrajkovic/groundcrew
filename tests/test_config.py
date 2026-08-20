from __future__ import annotations

from pathlib import Path

import pytest
from conftest import write_config, write_registry

from groundcrew import cli, config
from groundcrew.config import RepoSettings, _RegistryEntry


def test_no_file_means_current_behavior(sandbox: Path) -> None:
    cfg = config.load()

    assert cfg.root == sandbox / "projects"
    assert cfg.quiet_seconds == 900
    assert cfg.tick_seconds == 3600
    assert cfg.nightly_hour == 4
    assert cfg.post_pull_timeout == 600
    assert cfg.notify_command == ()
    assert cfg.defaults == RepoSettings()


def test_full_file_parses(sandbox: Path) -> None:
    write_config(
        sandbox,
        """
        [claude]
        spawn = "same-dir"
        capacity = 8
        permission_mode = "acceptEdits"
        create_session_in_dir = false

        [notify]
        command = ["my-notifier", "--flag"]

        [hooks]
        post_pull = ["mise", "install"]
        post_pull_timeout = 120

        [timing]
        quiet_seconds = 60
        tick_seconds = 600
        nightly_hour = 2
        """,
    )

    cfg = config.load()

    assert cfg.notify_command == ("my-notifier", "--flag")
    assert cfg.post_pull_timeout == 120
    assert cfg.quiet_seconds == 60
    assert cfg.tick_seconds == 600
    assert cfg.nightly_hour == 2
    assert cfg.defaults.spawn == "same-dir"
    assert cfg.defaults.capacity == 8
    assert cfg.defaults.create_session_in_dir is False
    assert cfg.defaults.post_pull == ("mise", "install")


def test_claude_bin_key_feeds_the_config(sandbox: Path) -> None:
    write_config(sandbox, '[claude]\nbin = "/opt/homebrew/bin/claude"\n')

    assert config.load().claude_bin == Path("/opt/homebrew/bin/claude")


def test_env_root_beats_file_root(sandbox: Path) -> None:
    write_config(sandbox, 'root = "/from/the/file"\n')

    assert config.load().root == sandbox / "projects"


def test_file_root_used_without_env(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROUNDCREW_ROOT")
    write_config(sandbox, 'root = "/from/the/file"\n')

    assert config.load().root == Path("/from/the/file")


def test_tilde_expansion(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(sandbox))
    monkeypatch.delenv("GROUNDCREW_ROOT")
    write_config(
        sandbox,
        """
        root = "~/stuff"

        [hooks]
        post_pull = ["~/bin/refresh", "--all"]
        """,
    )

    cfg = config.load()

    assert cfg.root == sandbox / "stuff"
    assert cfg.defaults.post_pull == (str(sandbox / "bin" / "refresh"), "--all")


def test_unknown_top_level_key_names_it(sandbox: Path) -> None:
    write_config(sandbox, "bogus = 1\n")

    with pytest.raises(config.ConfigError, match="bogus"):
        config.load()


def test_unknown_nested_key_names_the_path(sandbox: Path) -> None:
    write_config(sandbox, "[claude]\ncapactiy = 32\n")

    with pytest.raises(config.ConfigError, match=r"\[claude\].capactiy"):
        config.load()


def test_session_spawn_mode_rejected_with_reason(sandbox: Path) -> None:
    write_config(sandbox, '[claude]\nspawn = "session"\n')

    with pytest.raises(config.ConfigError, match="session"):
        config.load()


def test_bad_permission_mode_rejected(sandbox: Path) -> None:
    write_config(sandbox, '[claude]\npermission_mode = "yolo"\n')

    with pytest.raises(config.ConfigError, match="yolo"):
        config.load()


def test_wrong_type_rejected(sandbox: Path) -> None:
    write_config(sandbox, '[claude]\ncapacity = "many"\n')

    with pytest.raises(config.ConfigError, match=r"\[claude\].capacity"):
        config.load()


def test_create_session_in_dir_must_be_boolean(sandbox: Path) -> None:
    write_config(sandbox, '[claude]\ncreate_session_in_dir = "no"\n')

    with pytest.raises(config.ConfigError, match="must be a boolean"):
        config.load()


def test_command_must_be_list_of_strings(sandbox: Path) -> None:
    write_config(sandbox, '[notify]\ncommand = "not-a-list"\n')

    with pytest.raises(config.ConfigError, match=r"\[notify\].command"):
        config.load()


def test_invalid_toml_is_a_config_error(sandbox: Path) -> None:
    write_config(sandbox, "[claude\n")

    with pytest.raises(config.ConfigError):
        config.load()


# ── repos.toml: one entry per managed directory ─────────────────────────────


def test_entry_settings_round_trip_and_unset_ones_stay_unset(sandbox: Path) -> None:
    """`remove` rewrites every survivor, so an untouched entry must come back untouched."""
    plain = sandbox / "projects" / "plain"
    tuned = sandbox / "projects" / "tuned"

    config.save_registry(
        [
            _RegistryEntry(path=tuned, spawn="same-dir", create_session_in_dir=False),
            _RegistryEntry(path=plain),
        ]
    )

    text = config.registry_path().read_text()
    assert f'[[repos]]\npath = "{plain}"\n' in text
    assert (
        f'[[repos]]\npath = "{tuned}"\nspawn = "same-dir"\ncreate_session_in_dir = false\n' in text
    )
    assert "capacity" not in text  # defaults are never stamped into the file
    assert config.load_registry() == [
        _RegistryEntry(path=plain),
        _RegistryEntry(path=tuned, spawn="same-dir", create_session_in_dir=False),
    ]


def test_legacy_flat_list_still_loads(sandbox: Path) -> None:
    a, b = sandbox / "projects" / "a", sandbox / "projects" / "b"
    write_registry(f'repos = ["{a}", "{b}"]\n')

    assert config.load_registry() == [_RegistryEntry(path=a), _RegistryEntry(path=b)]


def test_saving_a_legacy_registry_upgrades_it(sandbox: Path) -> None:
    a, b = sandbox / "projects" / "a", sandbox / "projects" / "b"
    write_registry(f'repos = ["{b}", "{a}"]\n')

    config.save_registry(config.load_registry())

    text = config.registry_path().read_text()
    assert "repos = [" not in text
    assert text.count("[[repos]]") == 2
    assert config.load_registry() == [_RegistryEntry(path=a), _RegistryEntry(path=b)]


def test_entry_path_resolves_like_a_registry_path(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`add` registers the resolved path, so a hand-written entry has to match it."""
    monkeypatch.setenv("HOME", str(sandbox))
    target = sandbox / "projects" / "scratch"
    target.mkdir()
    link = sandbox / "link-to-scratch"
    link.symlink_to(target)
    write_registry(f'[[repos]]\npath = "{link}"\n\n[[repos]]\npath = "~/projects/other"\n')

    assert [e.path for e in config.load_registry()] == [target, sandbox / "projects" / "other"]


def test_unknown_key_in_an_entry_names_the_file_and_the_entry(sandbox: Path) -> None:
    write_registry('[[repos]]\npath = "/a"\n\n[[repos]]\npath = "/b"\nname_prefix = "nope"\n')

    with pytest.raises(config.ConfigError) as raised:
        config.load_registry()

    message = str(raised.value)
    assert "repos.toml" in message
    assert "[[repos]] #2: name_prefix is not a known key" in message
    # the legacy string arm of the union always fails too; that is not the user's problem
    assert "valid string" not in message


def test_global_only_keys_rejected_in_an_entry(sandbox: Path) -> None:
    write_registry('[[repos]]\npath = "/a"\nbin = "/somewhere/claude"\n')

    with pytest.raises(config.ConfigError, match="bin"):
        config.load_registry()


def test_session_spawn_mode_rejected_in_an_entry(sandbox: Path) -> None:
    write_registry('[[repos]]\npath = "/a"\nspawn = "session"\n')

    with pytest.raises(config.ConfigError, match="session"):
        config.load_registry()


def test_entry_without_a_path_says_so(sandbox: Path) -> None:
    write_registry('[[repos]]\nspawn = "same-dir"\n')

    with pytest.raises(config.ConfigError, match=r"\[\[repos\]\] #1: path"):
        config.load_registry()


def test_effective_layers_entry_settings_over_defaults(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(sandbox))
    defaults = RepoSettings(spawn="same-dir", capacity=8, post_pull=("mise", "install"))
    entry = _RegistryEntry(
        path=sandbox / "projects" / "x", permission_mode="plan", post_pull=["~/bin/refresh"]
    )

    settings = config.effective(defaults, entry)

    assert settings.permission_mode == "plan"
    assert settings.post_pull == (str(sandbox / "bin" / "refresh"),)
    assert settings.spawn == "same-dir"  # untouched fields keep the global value
    assert settings.capacity == 8
    # an entry that sets nothing is exactly the defaults; an empty list still overrides
    bare = _RegistryEntry(path=Path("/x"))
    assert config.effective(defaults, bare) == defaults
    assert config.effective(defaults, _RegistryEntry(path=Path("/x"), post_pull=[])).post_pull == ()


def test_registry_defaults_into_config_dir(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROUNDCREW_REGISTRY")

    assert config.registry_path() == sandbox / "config" / "repos.toml"


def test_root_resolves_like_a_registry_path(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery globs `root` and compares the hits against registered paths."""
    real = sandbox / "real-projects"
    real.mkdir()
    link = sandbox / "linked-projects"
    link.symlink_to(real)
    monkeypatch.setenv("GROUNDCREW_ROOT", str(link))

    assert config.load().root == real


def test_bad_config_exits_with_ex_config(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(sandbox, "bogus = 1\n")
    monkeypatch.setattr("sys.argv", ["groundcrew", "status"])

    assert cli.main() == config.EX_CONFIG
    assert "bogus" in capsys.readouterr().err


def test_a_broken_registry_reaches_the_user_as_a_message(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """repos.toml is read inside the commands, not by `load`; the message still has to land."""
    write_registry('[[repos]]\npath = "/a"\nbogus = 1\n')
    monkeypatch.setattr("sys.argv", ["groundcrew", "remove", "/a"])

    assert cli.main() == config.EX_CONFIG
    assert "repos.toml: [[repos]] #1: bogus is not a known key" in capsys.readouterr().err


# ── repos.toml: what a hand-written or hostile file may hold ────────────────


def test_registry_round_trips_what_a_path_is_allowed_to_contain(sandbox: Path) -> None:
    """A path is bytes, not ASCII.

    `save_registry` rewrites the whole file, so a single path it cannot spell
    takes every other entry down with it on the next read.
    """
    weird = [
        sandbox / "projects" / "rocket-🚀",
        sandbox / "projects" / 'quote"and\\backslash',
        sandbox / "projects" / "tab\tand\nnewline",
        sandbox / "projects" / "bell\x07",
    ]
    entries = [_RegistryEntry(path=p, post_pull=["say", "🚀 done"]) for p in weird]

    config.save_registry(entries)

    assert config.load_registry() == sorted(entries, key=lambda e: e.path)


def test_an_unparseable_registry_is_a_config_error(sandbox: Path) -> None:
    """The daemon reads it before its loop, outside the guard that keeps it up."""
    write_registry('[[repos]\npath = "/a"\n')

    with pytest.raises(config.ConfigError, match=r"repos\.toml"):
        config.load_registry()


@pytest.mark.parametrize("value", ["42", "true", "1.5", '["/a"]', "{ capacity = 1 }", "1979-05-27"])
def test_a_path_that_is_not_a_string_names_the_entry(sandbox: Path, value: str) -> None:
    write_registry(f"[[repos]]\npath = {value}\n")

    with pytest.raises(config.ConfigError, match=r"\[\[repos\]\] #1: path must be a string"):
        config.load_registry()


def test_an_empty_path_is_rejected_rather_than_resolved(sandbox: Path) -> None:
    """It would otherwise resolve to wherever the daemon happens to be running."""
    write_registry('[[repos]]\npath = ""\n')

    with pytest.raises(config.ConfigError, match=r"\[\[repos\]\] #1: path must not be empty"):
        config.load_registry()


def test_add_refuses_a_non_git_directory_under_worktree_spawn(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = sandbox / "projects" / "scratch"
    scratch.mkdir()

    assert cli.cmd_add(config.load(), [str(scratch)]) == 1
    out = capsys.readouterr().out
    assert "[[repos]]" in out
    assert f'path = "{scratch}"' in out
    assert 'spawn = "same-dir"' in out
    assert not config.load_registry()


def test_add_accepts_a_non_git_directory_whose_entry_says_same_dir(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch = sandbox / "projects" / "scratch"
    scratch.mkdir()
    write_registry(f'[[repos]]\npath = "{scratch}"\nspawn = "same-dir"\n')

    assert cli.cmd_add(config.load(), [str(scratch)]) == 0
    assert "freshness pulls are skipped" in capsys.readouterr().out
    assert config.load_registry() == [_RegistryEntry(path=scratch, spawn="same-dir")]
