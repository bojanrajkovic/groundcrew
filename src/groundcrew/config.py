"""Paths, tunables, and the repo registry.

Every external location is overridable through an environment variable so tests
can point groundcrew at a sandbox instead of the real home directory.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

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
MISE_TIMEOUT = 600
UPDATE_TIMEOUT = 600
TERMINATE_TIMEOUT = 60


def projects_root() -> Path:
    return Path(os.environ.get("GROUNDCREW_ROOT", str(Path.home() / "Projects")))


def registry_path() -> Path:
    default = projects_root() / "groundcrew" / "repos.toml"
    return Path(os.environ.get("GROUNDCREW_REGISTRY", str(default)))


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
