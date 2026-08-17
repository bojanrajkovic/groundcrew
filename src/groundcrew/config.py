"""Paths, tunables, the config file, and the repo registry.

Two files live in the config directory (ADR 0002): ``config.toml`` is
human-written and never rewritten by groundcrew; ``repos.toml`` is the
machine-written registry. Precedence for every setting: environment variable
(tests) > config file > default. No config file means the defaults below —
the pre-config behavior, unchanged.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

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
NOTIFY_TIMEOUT = 30
# Supervisor logs are mostly repainted frames, so this holds far more history
# than its size suggests once `groundcrew logs` has collapsed the duplicates.
LOG_MAX_BYTES = 2 * 1024 * 1024

EX_CONFIG = 78  # sysexits.h; the systemd unit declares it restart-preventing

Spawn = Literal["worktree", "same-dir"]
PermissionMode = Literal["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"]


class ConfigError(Exception):
    """A problem in config.toml, named precisely enough to fix from the message."""


def _reject_session_spawn(value: object) -> object:
    if value == "session":
        raise ValueError(
            '"session" is not supported — the supervisor must outlive individual '
            f"sessions (allowed: {', '.join(get_args(Spawn))})"
        )
    return value


class RepoSettings(BaseModel):
    """Effective per-repo settings; every field is overridable per repo."""

    model_config = ConfigDict(frozen=True)

    spawn: Annotated[Spawn, BeforeValidator(_reject_session_spawn)] = "worktree"
    capacity: int = 32
    permission_mode: PermissionMode = "bypassPermissions"
    create_session_in_dir: bool = True
    post_pull: tuple[str, ...] = ()


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: Path
    claude_bin: Path
    notify_command: tuple[str, ...] = ()
    quiet_seconds: int = QUIET_SECONDS
    tick_seconds: int = TICK_SECONDS
    nightly_hour: int = NIGHTLY_HOUR
    post_pull_timeout: int = POST_PULL_TIMEOUT
    defaults: RepoSettings = RepoSettings()
    overrides: dict[Path, RepoSettings] = {}

    def for_repo(self, repo: Path) -> RepoSettings:
        return self.overrides.get(repo, self.defaults)


def config_dir() -> Path:
    if env := os.environ.get("GROUNDCREW_CONFIG_DIR"):
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "groundcrew"


# ── the file schema: what a human may write in config.toml ──────────────────
# Strict (TOML already delivers exact types; no coercion surprises) and
# extra="forbid" so any unknown key fails the load with its path named.

_STRICT = ConfigDict(strict=True, extra="forbid")


class _ClaudeTable(BaseModel):
    model_config = _STRICT
    spawn: Annotated[Spawn, BeforeValidator(_reject_session_spawn)] | None = None
    capacity: int | None = None
    permission_mode: PermissionMode | None = None
    create_session_in_dir: bool | None = None
    bin: str | None = None


class _NotifyTable(BaseModel):
    model_config = _STRICT
    command: list[str] | None = None


class _HooksTable(BaseModel):
    model_config = _STRICT
    post_pull: list[str] | None = None
    post_pull_timeout: int | None = None


class _TimingTable(BaseModel):
    model_config = _STRICT
    quiet_seconds: int | None = None
    tick_seconds: int | None = None
    nightly_hour: int | None = None


class _RepoOverride(BaseModel):
    model_config = _STRICT
    spawn: Annotated[Spawn, BeforeValidator(_reject_session_spawn)] | None = None
    capacity: int | None = None
    permission_mode: PermissionMode | None = None
    create_session_in_dir: bool | None = None
    post_pull: list[str] | None = None


class _ConfigFile(BaseModel):
    model_config = _STRICT
    root: str | None = None
    claude: _ClaudeTable = _ClaudeTable()
    notify: _NotifyTable = _NotifyTable()
    hooks: _HooksTable = _HooksTable()
    timing: _TimingTable = _TimingTable()
    repos: dict[str, _RepoOverride] = {}


def _format_loc(loc: tuple[str | int, ...]) -> str:
    """("claude", "capacity") → [claude].capacity · ("repos", "/x", "bin") → [repos."/x"].bin"""
    if not loc:
        return "config.toml"
    parts = list(loc)
    if parts[0] == "repos" and len(parts) > 1:
        head = f'[repos."{parts[1]}"]'
        rest = parts[2:]
    elif len(parts) == 1:
        return str(parts[0])
    else:
        head = f"[{parts[0]}]"
        rest = parts[1:]
    out = head
    for part in rest:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


_TYPE_PHRASES = {
    "bool_type": "must be a boolean",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "string_type": "must be a string",
    "list_type": "must be an array of strings",
    "tuple_type": "must be an array of strings",
    "model_type": "must be a table",
    "dict_type": "must be a table",
}


def _translate(exc: ValidationError) -> ConfigError:
    lines = []
    for err in exc.errors():
        path = _format_loc(tuple(err["loc"]))
        kind = err["type"]
        if kind == "extra_forbidden":
            lines.append(f"config.toml: {path} is not a known key")
        elif kind == "literal_error":
            expected = err.get("ctx", {}).get("expected", "")
            lines.append(
                f"config.toml: {path} must be one of: {expected}, got {err.get('input')!r}"
            )
        elif kind == "value_error":
            msg = err["msg"].removeprefix("Value error, ")
            lines.append(f"config.toml: {path} {msg}")
        elif kind in _TYPE_PHRASES:
            lines.append(f"config.toml: {path} {_TYPE_PHRASES[kind]}")
        else:
            lines.append(f"config.toml: {path}: {err['msg']}")
    return ConfigError("\n".join(lines))


def _command(raw: list[str] | None) -> tuple[str, ...] | None:
    """A command array; argv[0] gets ~ expanded so config can point at scripts."""
    if raw is None:
        return None
    if not raw:
        return ()
    return (str(Path(raw[0]).expanduser()), *raw[1:])


def _or(value: int | None, default: int) -> int:
    return default if value is None else value


def load() -> Config:
    path = config_dir() / "config.toml"
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    try:
        file = _ConfigFile.model_validate(data)
    except ValidationError as exc:
        raise _translate(exc) from exc

    defaults = RepoSettings()
    updates: dict[str, object] = {
        k: v
        for k, v in {
            "spawn": file.claude.spawn,
            "capacity": file.claude.capacity,
            "permission_mode": file.claude.permission_mode,
            "create_session_in_dir": file.claude.create_session_in_dir,
            "post_pull": _command(file.hooks.post_pull),
        }.items()
        if v is not None
    }
    defaults = defaults.model_copy(update=updates)

    overrides: dict[Path, RepoSettings] = {}
    for raw_key, table in file.repos.items():
        table_updates: dict[str, object] = {
            k: v
            for k, v in {
                "spawn": table.spawn,
                "capacity": table.capacity,
                "permission_mode": table.permission_mode,
                "create_session_in_dir": table.create_session_in_dir,
                "post_pull": _command(table.post_pull),
            }.items()
            if v is not None
        }
        overrides[Path(raw_key).expanduser()] = defaults.model_copy(update=table_updates)

    env_root = os.environ.get("GROUNDCREW_ROOT")
    root = Path(env_root or file.root or str(Path.home() / "Projects")).expanduser()

    return Config(
        root=root,
        claude_bin=Path(file.claude.bin).expanduser() if file.claude.bin else claude_bin(),
        notify_command=_command(file.notify.command) or (),
        quiet_seconds=_or(file.timing.quiet_seconds, QUIET_SECONDS),
        tick_seconds=_or(file.timing.tick_seconds, TICK_SECONDS),
        nightly_hour=_or(file.timing.nightly_hour, NIGHTLY_HOUR),
        post_pull_timeout=_or(file.hooks.post_pull_timeout, POST_PULL_TIMEOUT),
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
