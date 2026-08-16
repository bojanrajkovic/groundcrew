from __future__ import annotations

from pathlib import Path

from conftest import add_origin_commit, clone, git, make_repo

from groundcrew import gitops
from groundcrew.gitops import PullKind


def test_ff_pull_on_clean_default_branch(tmp_path: Path) -> None:
    origin = make_repo(tmp_path / "origin")
    repo = clone(origin, tmp_path / "repo")
    add_origin_commit(origin)

    outcome = gitops.pull(repo)

    assert outcome.kind is PullKind.FF_PULLED
    assert outcome.moved
    assert not outcome.parked
    assert (repo / "new.txt").exists()


def test_untracked_files_do_not_count_as_dirty(tmp_path: Path) -> None:
    origin = make_repo(tmp_path / "origin")
    repo = clone(origin, tmp_path / "repo")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "junk.txt").write_text("untracked\n")
    add_origin_commit(origin)

    outcome = gitops.pull(repo)

    assert outcome.kind is PullKind.FF_PULLED
    assert outcome.moved


def test_parked_branch_updates_ref_without_touching_tree(tmp_path: Path) -> None:
    origin = make_repo(tmp_path / "origin")
    repo = clone(origin, tmp_path / "repo")
    git(repo, "checkout", "-qb", "feature")
    (repo / "wip.txt").write_text("wip\n")  # dirty tree on the parked branch
    add_origin_commit(origin)

    outcome = gitops.pull(repo)

    assert outcome.kind is PullKind.REF_UPDATED
    assert outcome.moved
    assert outcome.parked
    origin_tip = git(origin, "rev-parse", "main").stdout.strip()
    local_main = git(repo, "rev-parse", "main").stdout.strip()
    assert local_main == origin_tip
    assert (repo / "wip.txt").exists()
    assert not (repo / "new.txt").exists()  # working tree untouched


def test_dirty_default_branch_fetches_and_warns(tmp_path: Path) -> None:
    origin = make_repo(tmp_path / "origin")
    repo = clone(origin, tmp_path / "repo")
    (repo / "README.md").write_text("modified\n")  # tracked file modified
    add_origin_commit(origin)

    outcome = gitops.pull(repo)

    assert outcome.kind is PullKind.FETCHED_DIRTY
    assert not outcome.moved
    assert "uncommitted" in outcome.detail


def test_default_branch_fallback_without_origin_head(tmp_path: Path) -> None:
    origin = make_repo(tmp_path / "origin")
    repo = clone(origin, tmp_path / "repo")
    git(repo, "remote", "set-head", "origin", "--delete")

    assert gitops.default_branch(repo) == "main"


def test_no_remote_yields_no_default_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    assert not gitops.has_remote(repo)
    outcome = gitops.pull(repo)
    assert outcome.kind is PullKind.NO_DEFAULT_BRANCH


def test_spawned_worktrees_reports_dirty_state_and_unmerged_commits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = repo / ".claude" / "worktrees" / "bridge-cse_test"
    git(repo, "worktree", "add", "-q", "-b", "worktree-bridge-cse_test", str(wt))
    (wt / "scratch.txt").write_text("in-flight work\n")

    infos = gitops.spawned_worktrees(repo)
    assert len(infos) == 1
    assert infos[0].branch == "worktree-bridge-cse_test"
    assert infos[0].dirty_files == 1
    assert infos[0].unmerged_commits == 0

    git(wt, "add", "scratch.txt")
    git(wt, "commit", "-qm", "in-flight commit")
    infos = gitops.spawned_worktrees(repo)
    assert infos[0].unmerged_commits == 1
    assert infos[0].dirty_files == 0


def test_diverged_default_branch_is_not_a_failure(tmp_path: Path) -> None:
    origin = make_repo(tmp_path / "origin")
    repo = clone(origin, tmp_path / "repo")
    (repo / "local.txt").write_text("local\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "local-only commit")
    add_origin_commit(origin)

    outcome = gitops.pull(repo)

    assert outcome.kind is gitops.PullKind.DIVERGED
    assert "\n" not in outcome.detail  # summarized to one line for the status table
