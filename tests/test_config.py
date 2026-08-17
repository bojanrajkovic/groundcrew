from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config

from groundcrew import cli, config


def test_no_file_means_current_behavior(sandbox: Path) -> None:
    cfg = config.load()

    assert cfg.root == sandbox / "projects"
    assert cfg.quiet_seconds == 900
    assert cfg.tick_seconds == 3600
    assert cfg.nightly_hour == 4
    assert cfg.post_pull_timeout == 600
    assert cfg.notify_command == ()
    assert cfg.defaults == config.RepoSettings()
    assert cfg.overrides == {}


def test_full_file_parses_and_materializes_overrides(sandbox: Path) -> None:
    write_config(
        sandbox,
        """
        [claude]
        spawn = "same-dir"
        capacity = 8
        permission_mode = "acceptEdits"

        [notify]
        command = ["my-notifier", "--flag"]

        [hooks]
        post_pull = ["mise", "install"]
        post_pull_timeout = 120

        [timing]
        quiet_seconds = 60
        tick_seconds = 600
        nightly_hour = 2

        [repos."/somewhere/cautious"]
        permission_mode = "plan"
        post_pull = []
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
    assert cfg.defaults.post_pull == ("mise", "install")
    # the override is a complete RepoSettings: unset keys inherit the defaults
    override = cfg.for_repo(Path("/somewhere/cautious"))
    assert override.permission_mode == "plan"
    assert override.post_pull == ()
    assert override.spawn == "same-dir"
    assert override.capacity == 8
    # unknown repos get the defaults object itself
    assert cfg.for_repo(Path("/somewhere/else")) is cfg.defaults


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

        [repos."~/stuff/repo"]
        capacity = 1
        """,
    )

    cfg = config.load()

    assert cfg.root == sandbox / "stuff"
    assert cfg.defaults.post_pull == (str(sandbox / "bin" / "refresh"), "--all")
    assert cfg.for_repo(sandbox / "stuff" / "repo").capacity == 1


def test_unknown_top_level_key_names_it(sandbox: Path) -> None:
    write_config(sandbox, "bogus = 1\n")

    with pytest.raises(config.ConfigError, match="bogus"):
        config.load()


def test_unknown_nested_key_names_the_path(sandbox: Path) -> None:
    write_config(sandbox, "[claude]\ncapactiy = 32\n")

    with pytest.raises(config.ConfigError, match=r"\[claude\].capactiy"):
        config.load()


def test_unknown_override_key_names_the_path(sandbox: Path) -> None:
    write_config(sandbox, '[repos."/x"]\nname_prefix = "nope"\n')

    with pytest.raises(config.ConfigError, match="name_prefix"):
        config.load()


def test_global_only_keys_rejected_per_repo(sandbox: Path) -> None:
    write_config(sandbox, '[repos."/x"]\nbin = "/somewhere/claude"\n')

    with pytest.raises(config.ConfigError, match="bin"):
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


def test_command_must_be_list_of_strings(sandbox: Path) -> None:
    write_config(sandbox, '[notify]\ncommand = "not-a-list"\n')

    with pytest.raises(config.ConfigError, match=r"\[notify\].command"):
        config.load()


def test_invalid_toml_is_a_config_error(sandbox: Path) -> None:
    write_config(sandbox, "[claude\n")

    with pytest.raises(config.ConfigError):
        config.load()


def test_registry_defaults_into_config_dir(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROUNDCREW_REGISTRY")

    assert config.registry_path() == sandbox / "config" / "repos.toml"


def test_status_warns_on_override_for_unregistered_repo(
    sandbox: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(sandbox, '[repos."/never/registered"]\ncapacity = 1\n')
    state: dict[str, object] = {
        "updated_at": 0,
        "binary_version": None,
        "pending_rollout": None,
        "last_update_result": "",
        "registered": [],
        "unregistered": [],
        "repos": {},
    }
    (sandbox / "state").mkdir(exist_ok=True)
    (sandbox / "state" / "state.json").write_text(json.dumps(state))

    assert cli.cmd_status(config.load()) == 0
    assert "override for unregistered repo: /never/registered" in capsys.readouterr().out


def test_bad_config_exits_with_ex_config(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(sandbox, "bogus = 1\n")
    monkeypatch.setattr("sys.argv", ["groundcrew", "status"])

    assert cli.main() == config.EX_CONFIG
    assert "bogus" in capsys.readouterr().err
