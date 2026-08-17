"""Git operations: freshness pulls and worktree inspection.

Pull policy (per repo, per tick):
- checked out on the default branch and clean  -> `git pull --ff-only`
- checked out elsewhere                        -> `git fetch origin <def>:<def>`
  (updates the local default-branch ref without touching the working tree)
- on the default branch but dirty              -> plain `git fetch` + a warning;
  git refuses to move the checked-out branch under uncommitted work
"Dirty" means modified tracked files only — untracked files (like the
`.claude/worktrees/` directory remote-control creates) never block or warn.
"""

from __future__ import annotations

import enum
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from groundcrew.config import GIT_TIMEOUT

_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
}


def run_git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **_GIT_ENV},
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A hung git must surface as a failure, not an exception that skips
        # the caller's failure accounting.
        return subprocess.CompletedProcess(
            cmd, 124, "", f"git {args[0]} timed out after {timeout}s"
        )


def default_branch(repo: Path) -> str | None:
    head = run_git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0:
        return head.stdout.strip().removeprefix("origin/")
    for candidate in ("main", "master"):
        if run_git(repo, "show-ref", "-q", f"refs/remotes/origin/{candidate}").returncode == 0:
            return candidate
    return None


def current_branch(repo: Path) -> str | None:
    result = run_git(repo, "branch", "--show-current")
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch else None


def has_remote(repo: Path) -> bool:
    result = run_git(repo, "remote")
    return result.returncode == 0 and bool(result.stdout.strip())


def dirty_tracked(repo: Path) -> bool:
    result = run_git(repo, "status", "--porcelain", "--untracked-files=no")
    return bool(result.stdout.strip())


def branch_tip(repo: Path, branch: str) -> str | None:
    result = run_git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return result.stdout.strip() if result.returncode == 0 else None


class PullKind(enum.Enum):
    FF_PULLED = "ff-pulled"
    REF_UPDATED = "ref-updated"
    FETCHED_DIRTY = "fetched-dirty"
    NO_DEFAULT_BRANCH = "no-default-branch"
    DIVERGED = "diverged"  # local ref moved past origin; needs a human, not a retry
    FAILED = "failed"


@dataclass(frozen=True)
class PullOutcome:
    kind: PullKind
    detail: str
    moved: bool
    parked: bool


# Non-ff outcomes are a repo state, not an infrastructure failure; they must not
# feed the consecutive-failure alerting.
_DIVERGENCE_MARKERS = ("fast-forward", "refusing to fetch into branch")


def summarize(text: str, limit: int = 300) -> str:
    """One line of the interesting part of command stderr: fatal/error lines first."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    important = [line for line in lines if any(m in line for m in ("fatal:", "error:", "rejected"))]
    return " · ".join(important or lines)[:limit]


def pull(repo: Path) -> PullOutcome:
    branch = default_branch(repo)
    if branch is None:
        return PullOutcome(
            PullKind.NO_DEFAULT_BRANCH,
            "no origin/HEAD, origin/main or origin/master",
            moved=False,
            parked=False,
        )

    before = branch_tip(repo, branch)
    checked_out = current_branch(repo)
    parked = checked_out != branch

    if not parked and not dirty_tracked(repo):
        result = run_git(repo, "pull", "--ff-only", "--quiet")
        ok_kind = PullKind.FF_PULLED
        ok_detail = ""
    elif parked:
        result = run_git(repo, "fetch", "origin", f"{branch}:{branch}")
        ok_kind = PullKind.REF_UPDATED
        ok_detail = ""
    else:
        result = run_git(repo, "fetch", "origin", "--quiet")
        ok_kind = PullKind.FETCHED_DIRTY
        ok_detail = f"uncommitted work on {branch}; local ref not advanced"

    if result.returncode == 0:
        kind, detail = ok_kind, ok_detail
    elif any(m in result.stderr for m in _DIVERGENCE_MARKERS):
        kind, detail = PullKind.DIVERGED, summarize(result.stderr)
    else:
        kind, detail = PullKind.FAILED, summarize(result.stderr)

    after = branch_tip(repo, branch)
    return PullOutcome(kind, detail, moved=before != after, parked=parked)


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str | None
    dirty_files: int
    unmerged_commits: int  # commits on the worktree branch not reachable from the repo's HEAD
    age_days: float


def worktree_dirty_listing(wt: WorktreeInfo) -> str:
    """`git status --short` for the worktree, for display before deletion."""
    return run_git(wt.path, "status", "--short").stdout.rstrip()


def worktree_unmerged_listing(repo: Path, wt: WorktreeInfo) -> str:
    """One line per commit that deleting the worktree's branch would orphan."""
    if not wt.branch:
        return ""
    return run_git(repo, "log", "--oneline", f"HEAD..{wt.branch}").stdout.rstrip()


def remove_worktree(repo: Path, wt: WorktreeInfo) -> str | None:
    """Force-remove a spawned worktree and delete its branch.

    Returns an error summary, or None on success. Branch deletion failure is
    deliberately non-fatal: the worktree is already gone, and an orphaned
    branch shows up in `git branch` rather than silently costing disk.
    """
    removed = run_git(repo, "worktree", "remove", "--force", str(wt.path))
    if removed.returncode != 0:
        return summarize(removed.stderr) or "worktree remove failed"
    if wt.branch:
        run_git(repo, "branch", "-D", wt.branch)
    return None


def spawned_worktrees(repo: Path) -> list[WorktreeInfo]:
    """Worktrees remote-control created under <repo>/.claude/worktrees/."""
    base = repo / ".claude" / "worktrees"
    if not base.is_dir():
        return []
    now = time.time()
    out: list[WorktreeInfo] = []
    for wt in sorted(base.iterdir()):
        if not wt.is_dir():
            continue
        status = run_git(wt, "status", "--porcelain")
        dirty = len(status.stdout.strip().splitlines()) if status.returncode == 0 else 0
        branch = current_branch(wt)
        unmerged = 0
        if branch:
            count = run_git(repo, "rev-list", "--count", f"HEAD..{branch}")
            unmerged = int(count.stdout.strip()) if count.returncode == 0 else 0
        out.append(
            WorktreeInfo(
                path=wt,
                branch=branch,
                dirty_files=dirty,
                unmerged_commits=unmerged,
                age_days=(now - wt.stat().st_mtime) / 86400,
            )
        )
    return out
