"""Paths, tunables, the config file, and the repo registry.

Two files live in the config directory (ADR 0002): ``config.toml`` is
human-written, never rewritten by groundcrew, and holds global settings only;
``repos.toml`` is the machine-written registry, one entry per managed
directory carrying that directory's own settings. Precedence for every
setting: environment variable (tests) > registry entry > config file >
default. No config file means the defaults below — the pre-config behavior,
unchanged.
"""

from __future__ import annotations

import json
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
# A deferred stop is normal. One still deferred a day later means the repo is
# stuck on its launched version.
STOP_DEFER_ALERT_SECONDS = 24 * 3600
# A supervisor creates its in-dir session after it connects. Long enough that a
# slow connect is not mistaken for a supervisor that failed to create one.
ANCHOR_GRACE_SECONDS = 5 * 60
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
    """A problem in a config file, named precisely enough to fix from the message."""


def _reject_session_spawn(value: object) -> object:
    if value == "session":
        raise ValueError(
            '"session" is not supported — the supervisor must outlive individual '
            f"sessions (allowed: {', '.join(get_args(Spawn))})"
        )
    return value


def repo_path(raw: object) -> Path:
    """The canonical spelling of a managed directory.

    The registry and `root` are both keyed by directory identity, so both have
    to spell a path the same way. `resolve()` collapses a symlink and its
    target to one key, and is non-strict, so a path that does not exist yet
    still normalizes.

    Also the trust boundary for `path`, which comes straight out of TOML a
    human wrote: `Path(42)` raises TypeError, which pydantic does not trap, and
    `Path("")` resolves to whatever directory the process happens to be in.
    Both are rejected here as ValueError, so the entry and key get named.
    """
    if not isinstance(raw, str | Path):
        # ValueError, not the TypeError this reads like: pydantic traps only
        # ValueError and AssertionError, and a TypeError here would escape
        # model_validate, load_registry, and main's handler alike.
        raise ValueError(f"must be a string, got {type(raw).__name__}")  # noqa: TRY004
    if not str(raw):
        raise ValueError("must not be empty")
    return Path(raw).expanduser().resolve()


RepoKey = Annotated[Path, BeforeValidator(repo_path)]
"""A directory path that normalizes itself — no call site can forget to."""


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

    root: RepoKey
    claude_bin: Path
    notify_command: tuple[str, ...] = ()
    quiet_seconds: int = QUIET_SECONDS
    tick_seconds: int = TICK_SECONDS
    nightly_hour: int = NIGHTLY_HOUR
    post_pull_timeout: int = POST_PULL_TIMEOUT
    defaults: RepoSettings = RepoSettings()


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


class _ConfigFile(BaseModel):
    model_config = _STRICT
    root: str | None = None
    claude: _ClaudeTable = _ClaudeTable()
    notify: _NotifyTable = _NotifyTable()
    hooks: _HooksTable = _HooksTable()
    timing: _TimingTable = _TimingTable()


class _RegistryEntry(BaseModel):
    """One managed directory as repos.toml spells it: a path, plus only what was set.

    Unset settings stay None rather than picking up the globals: `remove`
    rewrites every surviving entry, and merged-in defaults would be stamped
    into the file as if a human had chosen them. `effective` does the merge at
    the two places that need whole settings.
    """

    model_config = _STRICT
    path: RepoKey
    spawn: Annotated[Spawn, BeforeValidator(_reject_session_spawn)] | None = None
    capacity: int | None = None
    permission_mode: PermissionMode | None = None
    create_session_in_dir: bool | None = None
    post_pull: list[str] | None = None


class _RegistryFile(BaseModel):
    model_config = _STRICT
    # The string arm is the pre-entry `repos = ["/a", "/b"]` format: it still
    # loads, and the next save writes it back as [[repos]] tables.
    repos: list[_RegistryEntry | str] = []


def _keys(parts: list[str | int]) -> str:
    """('post_pull', 1) → .post_pull[1]"""
    return "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


def _format_loc(loc: tuple[str | int, ...]) -> str:
    """("claude", "capacity") → [claude].capacity · ("repos", 1, arm, "spawn") → [[repos]] #2: spawn

    A registry entry validates against a union (an entry table or a legacy bare
    string), so pydantic names the arm it matched in the middle of the path.
    Drop it, and count the entries the way a human reading the file does.
    """
    parts = list(loc)
    if len(parts) > 1 and parts[0] == "repos" and isinstance(parts[1], int):
        entry = f"[[repos]] #{parts[1] + 1}"
        keys = _keys(parts[3:]).removeprefix(".")
        return f"{entry}: {keys}" if keys else entry
    if not parts:
        return "the file"
    if len(parts) == 1:
        return str(parts[0])
    return f"[{parts[0]}]" + _keys(parts[1:])


_TYPE_PHRASES = {
    "missing": "is required",
    "bool_type": "must be a boolean",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "string_type": "must be a string",
    "list_type": "must be an array of strings",
    "tuple_type": "must be an array of strings",
    "model_type": "must be a table",
    "dict_type": "must be a table",
}


def _translate(exc: ValidationError, filename: str) -> ConfigError:
    lines = []
    for err in exc.errors():
        loc = tuple(err["loc"])
        if loc[-1:] == ("str",):
            # An entry that is not a legacy path string; the entry arm says why.
            continue
        path = _format_loc(loc)
        kind = err["type"]
        if kind == "extra_forbidden":
            lines.append(f"{filename}: {path} is not a known key")
        elif kind == "literal_error":
            expected = err.get("ctx", {}).get("expected", "")
            lines.append(f"{filename}: {path} must be one of: {expected}, got {err.get('input')!r}")
        elif kind == "value_error":
            msg = err["msg"].removeprefix("Value error, ")
            lines.append(f"{filename}: {path} {msg}")
        elif kind in _TYPE_PHRASES:
            lines.append(f"{filename}: {path} {_TYPE_PHRASES[kind]}")
        else:
            lines.append(f"{filename}: {path}: {err['msg']}")
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
        raise _translate(exc, "config.toml") from exc

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

    return Config(
        root=Path(os.environ.get("GROUNDCREW_ROOT") or file.root or Path.home() / "Projects"),
        claude_bin=Path(file.claude.bin).expanduser() if file.claude.bin else claude_bin(),
        notify_command=_command(file.notify.command) or (),
        quiet_seconds=_or(file.timing.quiet_seconds, QUIET_SECONDS),
        tick_seconds=_or(file.timing.tick_seconds, TICK_SECONDS),
        nightly_hour=_or(file.timing.nightly_hour, NIGHTLY_HOUR),
        post_pull_timeout=_or(file.hooks.post_pull_timeout, POST_PULL_TIMEOUT),
        defaults=defaults,
    )


def effective(defaults: RepoSettings, entry: _RegistryEntry) -> RepoSettings:
    """Global defaults with this entry's explicit settings laid over them."""
    updates = entry.model_dump(exclude_none=True, exclude={"path"})
    if "post_pull" in updates:
        updates["post_pull"] = _command(updates["post_pull"])
    return defaults.model_copy(update=updates)


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


def load_registry() -> list[_RegistryEntry]:
    """Every managed directory, carrying only the settings its entry states."""
    path = registry_path()
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        file = _RegistryFile.model_validate(data)
    except ValidationError as exc:
        raise _translate(exc, path.name) from exc
    return [_RegistryEntry(path=Path(e)) if isinstance(e, str) else e for e in file.repos]


def save_registry(entries: list[_RegistryEntry]) -> None:
    """Rewrite repos.toml: one [[repos]] table per directory, sorted by path.

    Only what an entry actually sets is re-emitted, so an entry nobody touched
    comes back exactly as it was written.
    """
    latest = {e.path: e for e in entries}  # a repeated path is one directory, last write wins
    lines = [
        "# Directories managed by groundcrew, and their per-directory settings.",
        "# Written by `groundcrew add` / `groundcrew remove`; comments are not preserved.",
    ]
    for entry in sorted(latest.values(), key=lambda e: e.path):
        # TOML basic strings and JSON strings share their escape syntax, and
        # every field here is a string, bool, int, or array of strings.
        # ensure_ascii=False: the ASCII form spells a non-BMP character as a
        # UTF-16 surrogate pair, which TOML rejects, and one such path would
        # take the whole rewritten file down with it.
        fields = entry.model_dump(mode="json", exclude_none=True)
        lines += [
            "",
            "[[repos]]",
            *(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()),
        ]
    registry_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(registry_path(), "\n".join(lines) + "\n")
