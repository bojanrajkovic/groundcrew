from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from groundcrew.config import claude_home


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every groundcrew path at a throwaway directory."""
    monkeypatch.setenv("GROUNDCREW_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("GROUNDCREW_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GROUNDCREW_REGISTRY", str(tmp_path / "repos.toml"))
    monkeypatch.setenv("GROUNDCREW_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("GROUNDCREW_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("GROUNDCREW_CLAUDE_JSON", str(tmp_path / "claude.json"))
    (tmp_path / "projects").mkdir()
    return tmp_path


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def make_repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", branch, "-q", str(path)], check=True)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "init")
    return path


def clone(origin: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True, capture_output=True)
    git(dest, "config", "user.email", "test@example.com")
    git(dest, "config", "user.name", "Test")
    return dest


def add_origin_commit(origin: Path, filename: str = "new.txt") -> None:
    (origin / filename).write_text("more\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", f"add {filename}")


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def write_config(sandbox: Path, body: str) -> None:
    cfg_dir = sandbox / "config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.toml").write_text(body)


def write_session(
    pid: int,
    session_id: str,
    cwd: str,
    started_at_ms: int,
    *,
    entrypoint: str | None = None,
) -> None:
    """Write the metadata file a running engine leaves in ~/.claude/sessions."""
    sessions = claude_home() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": started_at_ms,
        "version": "2.1.233",
    }
    if entrypoint is not None:
        data["entrypoint"] = entrypoint
    (sessions / f"{pid}.json").write_text(json.dumps(data))
