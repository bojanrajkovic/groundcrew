"""Paths, tunables, the config file, and the repo registry.

Two files live in the config directory (ADR 0002): ``config.toml`` is
human-written and never rewritten by groundcrew; ``repos.toml`` is the
machine-written registry. Precedence for every setting: environment variable
(tests) > config file > default. No config file means the defaults below —
the pre-config behavior, unchanged.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

QUIET_SECONDS = 15 * 60
TICK_SECONDS = 3600
POLL_SECONDS = 30
NIGHTLY_HOUR = 4
CRASH_WINDOW_SECONDS = 10 * 60
CRASH_LIMIT = 3
BACKOFF_SECONDS = 30 * 60
# Registering many environments at once trips the API's rate limit (429) and
# the rejected supervisors exit; ramp spawns instead of stampeding.
MAX_SPAWNS_PER_PASS = 3
PULL_FAILURES_BEFORE_ALERT = 3
GIT_TIMEOUT = 120
POST_PULL_TIMEOUT = 600
UPDATE_TIMEOUT = 600
TERMINATE_TIMEOUT = 60

EX_CONFIG = 78  # sysexits.h; the systemd unit declares it restart-preventing

Spawn = Literal["worktree", "same-dir"]
PermissionMode = Literal["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"]


class ConfigError(Exception):
    """A problem in config.toml, named precisely enough to fix from the message."""


@dataclass(frozen=True)
class RepoSettings:
    """Effective per-repo settings; every field is overridable per repo."""

    spawn: Spawn = "worktree"
    capacity: int = 32
    permission_mode: PermissionMode = "bypassPermissions"
    post_pull: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    root: Path
    claude_bin: Path
    notify_command: tuple[str, ...] = ()
    quiet_seconds: int = QUIET_SECONDS
    tick_seconds: int = TICK_SECONDS
    nightly_hour: int = NIGHTLY_HOUR
    post_pull_timeout: int = POST_PULL_TIMEOUT
    defaults: RepoSettings = RepoSettings()
    overrides: dict[Path, RepoSettings] = field(default_factory=dict)

    def for_repo(self, repo: Path) -> RepoSettings:
        return self.overrides.get(repo, self.defaults)


def config_dir() -> Path:
    if env := os.environ.get("GROUNDCREW_CONFIG_DIR"):
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "groundcrew"


def _check_keys(section: dict[str, object], allowed: tuple[str, ...], where: str) -> None:
    for key in section:
        if key not in allowed:
            raise ConfigError(f"config.toml: {where}{key} is not a known key")


def _table(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"config.toml: {where} must be a table")
    return value


def _str(section: dict[str, object], key: str, where: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"config.toml: {where}{key} must be a string")
    return value


def _int(section: dict[str, object], key: str, where: str) -> int | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"config.toml: {where}{key} must be an integer")
    return value


def _int_or(section: dict[str, object], key: str, where: str, default: int) -> int:
    value = _int(section, key, where)
    return default if value is None else value


def _command(section: dict[str, object], key: str, where: str) -> tuple[str, ...] | None:
    """A command array; argv[0] gets ~ expanded so config can point at scripts."""
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"config.toml: {where}{key} must be an array of strings")
    if not value:
        return ()
    return (str(Path(value[0]).expanduser()), *value[1:])


def _repo_settings(section: dict[str, object], base: RepoSettings, where: str) -> RepoSettings:
    _check_keys(section, ("spawn", "capacity", "permission_mode", "post_pull"), where)
    updates: dict[str, object] = {}
    if (spawn := _str(section, "spawn", where)) is not None:
        if spawn == "session":
            raise ConfigError(
                f'config.toml: {where}spawn "session" is not supported — the '
                f"supervisor must outlive individual sessions "
                f"(allowed: {', '.join(get_args(Spawn))})"
            )
        if spawn not in get_args(Spawn):
            raise ConfigError(
                f"config.toml: {where}spawn must be one of: {', '.join(get_args(Spawn))}, "
                f"got {spawn!r}"
            )
        updates["spawn"] = spawn
    if (capacity := _int(section, "capacity", where)) is not None:
        updates["capacity"] = capacity
    if (mode := _str(section, "permission_mode", where)) is not None:
        if mode not in get_args(PermissionMode):
            raise ConfigError(
                f"config.toml: {where}permission_mode must be one of: "
                f"{', '.join(get_args(PermissionMode))}, got {mode!r}"
            )
        updates["permission_mode"] = mode
    if (post_pull := _command(section, "post_pull", where)) is not None:
        updates["post_pull"] = post_pull
    return dataclasses.replace(base, **updates)  # type: ignore[arg-type]


def load() -> Config:
    path = config_dir() / "config.toml"
    try:
        data: dict[str, object] = tomllib.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    _check_keys(data, ("root", "claude", "notify", "hooks", "timing", "repos"), "")
    claude = _table(data.get("claude", {}), "[claude]")
    _check_keys(claude, ("spawn", "capacity", "permission_mode", "bin"), "[claude].")
    notify = _table(data.get("notify", {}), "[notify]")
    _check_keys(notify, ("command",), "[notify].")
    hooks = _table(data.get("hooks", {}), "[hooks]")
    _check_keys(hooks, ("post_pull", "post_pull_timeout"), "[hooks].")
    timing = _table(data.get("timing", {}), "[timing]")
    _check_keys(timing, ("quiet_seconds", "tick_seconds", "nightly_hour"), "[timing].")

    defaults = _repo_settings(
        {k: v for k, v in claude.items() if k != "bin"}, RepoSettings(), "[claude]."
    )
    if "post_pull" in hooks:
        defaults = _repo_settings({"post_pull": hooks["post_pull"]}, defaults, "[hooks].")

    overrides: dict[Path, RepoSettings] = {}
    for raw_key, raw_table in _table(data.get("repos", {}), "[repos]").items():
        where = f'[repos."{raw_key}"].'
        table = _table(raw_table, f'[repos."{raw_key}"]')
        overrides[Path(raw_key).expanduser()] = _repo_settings(table, defaults, where)

    env_root = os.environ.get("GROUNDCREW_ROOT")
    file_root = _str(data, "root", "")
    root = Path(env_root or file_root or str(Path.home() / "Projects")).expanduser()

    file_bin = _str(claude, "bin", "[claude].")
    return Config(
        root=root,
        claude_bin=Path(file_bin).expanduser() if file_bin else claude_bin(),
        notify_command=_command(notify, "command", "[notify].") or (),
        quiet_seconds=_int_or(timing, "quiet_seconds", "[timing].", QUIET_SECONDS),
        tick_seconds=_int_or(timing, "tick_seconds", "[timing].", TICK_SECONDS),
        nightly_hour=_int_or(timing, "nightly_hour", "[timing].", NIGHTLY_HOUR),
        post_pull_timeout=_int_or(hooks, "post_pull_timeout", "[hooks].", POST_PULL_TIMEOUT),
        defaults=defaults,
        overrides=overrides,
    )


def registry_path() -> Path:
    if env := os.environ.get("GROUNDCREW_REGISTRY"):
        return Path(env)
    return config_dir() / "repos.toml"


def state_dir() -> Path:
    default = Path.home() / ".local" / "state" / "groundcrew"
    d = Path(os.environ.get("GROUNDCREW_STATE", str(default)))
    d.mkdir(parents=True, exist_ok=True)
    return d


def claude_home() -> Path:
    return Path(os.environ.get("GROUNDCREW_CLAUDE_HOME", str(Path.home() / ".claude")))


def claude_json_path() -> Path:
    return Path(os.environ.get("GROUNDCREW_CLAUDE_JSON", str(Path.home() / ".claude.json")))


def claude_bin() -> Path:
    """The native-installer binary, never a PATH lookup.

    The daemon's PATH has mise shims first, and a repo's mise config can pin its
    own `claude` (observed: an npm-installed 2.1.167 shadowing native 2.1.233).
    Supervisors must all run the one binary that version-drift tracks.
    """
    native = Path.home() / ".local" / "bin" / "claude"
    if native.exists():
        return native
    found = shutil.which("claude")
    return Path(found) if found else native


def mise_bin() -> Path:
    found = shutil.which("mise")
    return Path(found) if found else Path.home() / ".local" / "bin" / "mise"


def atomic_write(path: Path, content: str) -> None:
    """Write via temp file + rename so readers never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def load_registry() -> list[Path]:
    path = registry_path()
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        raise TypeError(f"{path}: 'repos' must be a list")
    out: list[Path] = []
    for entry in repos:
        if not isinstance(entry, str):
            raise TypeError(f"{path}: repo entries must be strings, got {entry!r}")
        out.append(Path(entry))
    return out


def save_registry(repos: list[Path]) -> None:
    lines = [
        "# Repositories managed by groundcrew.",
        "# Maintained by `groundcrew add` / `groundcrew remove`; hand-editing is fine too.",
        "repos = [",
        *(f'    "{r}",' for r in sorted({str(r) for r in repos})),
        "]",
    ]
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(registry_path(), "\n".join(lines) + "\n")
