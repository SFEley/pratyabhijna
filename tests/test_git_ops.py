"""Tests for git_ops against real git in a tempdir.

git_ops is a thin wrapper; mocking subprocess would test the wrapper,
not the behavior. Real git in tempdirs is fast (< 1s per test) and
gives true confidence. No network, no shared state.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from pratyabhijna import git_ops
from pratyabhijna.git_ops import GitError


# --- Helpers ---


def _sync_git(repo: Path, *args: str) -> str:
    """Synchronous git call for test setup."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _init_repo(repo: Path) -> None:
    """Create an initialized repo with an initial commit on main."""
    repo.mkdir(parents=True, exist_ok=True)
    _sync_git(repo, "init", "--initial-branch=main")
    _sync_git(repo, "config", "user.email", "test@example.com")
    _sync_git(repo, "config", "user.name", "Test")
    _sync_git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("initial\n")
    _sync_git(repo, "add", "README.md")
    _sync_git(repo, "commit", "-m", "initial")


@pytest.fixture
def repo(tmp_path):
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


# --- Branch queries ---


@pytest.mark.asyncio
async def test_current_branch_returns_main(repo):
    assert await git_ops.current_branch(repo) == "main"


@pytest.mark.asyncio
async def test_branch_exists_true_for_current(repo):
    assert await git_ops.branch_exists(repo, "main") is True


@pytest.mark.asyncio
async def test_branch_exists_false_for_missing(repo):
    assert await git_ops.branch_exists(repo, "nope") is False


@pytest.mark.asyncio
async def test_is_dirty_false_on_clean_repo(repo):
    assert await git_ops.is_dirty(repo) is False


@pytest.mark.asyncio
async def test_is_dirty_true_with_untracked_file(repo):
    (repo / "new.md").write_text("x")
    assert await git_ops.is_dirty(repo) is True


# --- Branch operations ---


@pytest.mark.asyncio
async def test_create_branch_from_main(repo):
    await git_ops.create_branch(repo, "synth/draft", base="main")
    assert await git_ops.current_branch(repo) == "synth/draft"
    assert await git_ops.branch_exists(repo, "synth/draft") is True


@pytest.mark.asyncio
async def test_create_branch_fails_if_already_exists(repo):
    await git_ops.create_branch(repo, "synth/draft", base="main")
    await git_ops.checkout(repo, "main")
    with pytest.raises(GitError):
        await git_ops.create_branch(repo, "synth/draft", base="main")


@pytest.mark.asyncio
async def test_checkout_switches_branches(repo):
    await git_ops.create_branch(repo, "feature", base="main")
    await git_ops.checkout(repo, "main")
    assert await git_ops.current_branch(repo) == "main"


@pytest.mark.asyncio
async def test_delete_branch(repo):
    await git_ops.create_branch(repo, "tmp", base="main")
    await git_ops.checkout(repo, "main")
    await git_ops.delete_branch(repo, "tmp")
    assert await git_ops.branch_exists(repo, "tmp") is False


@pytest.mark.asyncio
async def test_delete_branch_force_for_unmerged(repo):
    await git_ops.create_branch(repo, "tmp", base="main")
    (repo / "x.md").write_text("x")
    await git_ops.add(repo, "x.md")
    await git_ops.commit(repo, "add x")
    await git_ops.checkout(repo, "main")
    # Unmerged — regular delete should fail, force should succeed
    with pytest.raises(GitError):
        await git_ops.delete_branch(repo, "tmp", force=False)
    await git_ops.delete_branch(repo, "tmp", force=True)
    assert await git_ops.branch_exists(repo, "tmp") is False


# --- Commit operations ---


@pytest.mark.asyncio
async def test_add_and_commit_returns_sha(repo):
    (repo / "file.md").write_text("hello")
    await git_ops.add(repo, "file.md")
    sha = await git_ops.commit(repo, "add file")
    assert len(sha) == 40
    log = _sync_git(repo, "log", "--oneline", "-1")
    assert "add file" in log


@pytest.mark.asyncio
async def test_add_requires_at_least_one_path(repo):
    with pytest.raises(ValueError):
        await git_ops.add(repo)


@pytest.mark.asyncio
async def test_commit_fails_on_empty_staging_by_default(repo):
    with pytest.raises(GitError):
        await git_ops.commit(repo, "empty")


@pytest.mark.asyncio
async def test_commit_allow_empty(repo):
    sha = await git_ops.commit(repo, "empty ok", allow_empty=True)
    assert len(sha) == 40


# --- Rebase ---


@pytest.mark.asyncio
async def test_rebase_onto_clean_fast_forward(repo):
    # feature branched from main, main advances, feature rebases onto main
    await git_ops.create_branch(repo, "feature", base="main")
    (repo / "f.md").write_text("feature work")
    await git_ops.add(repo, "f.md")
    await git_ops.commit(repo, "feature commit")

    await git_ops.checkout(repo, "main")
    (repo / "m.md").write_text("main work")
    await git_ops.add(repo, "m.md")
    await git_ops.commit(repo, "main commit")

    await git_ops.checkout(repo, "feature")
    await git_ops.rebase_onto(repo, "main")

    # After rebase, feature should contain both files
    assert (repo / "f.md").exists()
    assert (repo / "m.md").exists()


@pytest.mark.asyncio
async def test_rebase_conflict_raises_and_abort_recovers(repo):
    # feature and main both edit README -> conflict on rebase
    await git_ops.create_branch(repo, "feature", base="main")
    (repo / "README.md").write_text("feature version\n")
    await git_ops.add(repo, "README.md")
    await git_ops.commit(repo, "feature change")

    await git_ops.checkout(repo, "main")
    (repo / "README.md").write_text("main version\n")
    await git_ops.add(repo, "README.md")
    await git_ops.commit(repo, "main change")

    await git_ops.checkout(repo, "feature")
    with pytest.raises(GitError):
        await git_ops.rebase_onto(repo, "main")

    await git_ops.rebase_abort(repo)
    # After abort, we're back on feature with no rebase in progress
    assert await git_ops.current_branch(repo) == "feature"
    assert await git_ops.is_dirty(repo) is False


# --- Diff / log ---


@pytest.mark.asyncio
async def test_diff_returns_diff_text(repo):
    await git_ops.create_branch(repo, "feature", base="main")
    (repo / "new.md").write_text("content\n")
    await git_ops.add(repo, "new.md")
    await git_ops.commit(repo, "add new")

    text = await git_ops.diff(repo, "main", "feature")
    assert "new.md" in text
    assert "+content" in text


@pytest.mark.asyncio
async def test_log_since_returns_commits_on_branch(repo):
    await git_ops.create_branch(repo, "feature", base="main")
    (repo / "a.md").write_text("a")
    await git_ops.add(repo, "a.md")
    await git_ops.commit(repo, "first")
    (repo / "b.md").write_text("b")
    await git_ops.add(repo, "b.md")
    await git_ops.commit(repo, "second")

    lines = await git_ops.log_since(repo, "main", "feature")
    assert len(lines) == 2
    # Most recent first
    assert "second" in lines[0]
    assert "first" in lines[1]


@pytest.mark.asyncio
async def test_log_since_empty_when_no_divergence(repo):
    await git_ops.create_branch(repo, "feature", base="main")
    lines = await git_ops.log_since(repo, "main", "feature")
    assert lines == []


# --- Error surface ---


@pytest.mark.asyncio
async def test_git_error_includes_stderr(tmp_path):
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    with pytest.raises(GitError) as exc_info:
        await git_ops.current_branch(not_a_repo)
    assert exc_info.value.returncode != 0
    assert exc_info.value.stderr  # non-empty
