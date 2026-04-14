"""Async wrappers around the ``git`` CLI for synthesizer operations.

The synthesizer touches a repo that is NOT the Pratyabhijna server's
own working directory — it operates on the subject's repo (path comes
from ``config.repo_path``). Every function here takes ``repo_path`` as
an explicit argument; nothing depends on process cwd.

These are thin wrappers, not a git abstraction. Keep them small. When
a command fails, raise ``GitError`` with stderr attached — callers
decide whether to surface, log, or recover.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git command exits non-zero."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        super().__init__(f"git {' '.join(cmd)} failed ({returncode}): {stderr.strip()}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


async def _run(
    repo_path: str | Path,
    *args: str,
    check: bool = True,
) -> CommandResult:
    """Run a git command inside ``repo_path``.

    Raises ``GitError`` on non-zero exit when ``check`` is True (default).
    Set ``check=False`` for commands whose non-zero exit is informational
    (e.g. ``git rev-parse --verify`` used as an existence probe).
    """
    path = Path(repo_path).expanduser().resolve()
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode or -1, stderr)
    return CommandResult(stdout=stdout, stderr=stderr, returncode=proc.returncode or 0)


# --- Branch queries ---


async def current_branch(repo_path: str | Path) -> str:
    """Return the currently checked-out branch name."""
    result = await _run(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


async def branch_exists(repo_path: str | Path, name: str) -> bool:
    """Whether a local branch with this name exists."""
    result = await _run(
        repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False
    )
    return result.returncode == 0


async def is_dirty(repo_path: str | Path) -> bool:
    """Whether the working tree has uncommitted changes."""
    result = await _run(repo_path, "status", "--porcelain")
    return bool(result.stdout.strip())


# --- Branch operations ---


async def checkout(repo_path: str | Path, ref: str) -> None:
    """Check out an existing branch, tag, or commit."""
    await _run(repo_path, "checkout", ref)


async def create_branch(
    repo_path: str | Path,
    name: str,
    base: str = "main",
) -> None:
    """Create ``name`` from ``base`` and check it out.

    Raises GitError if the branch already exists — callers should check
    first with ``branch_exists`` when the singleton-or-append pattern is
    needed.
    """
    await checkout(repo_path, base)
    await _run(repo_path, "checkout", "-b", name)


async def rebase_onto(repo_path: str | Path, onto: str) -> None:
    """Rebase the current branch onto ``onto`` (e.g. 'main').

    Conflicts raise GitError with stderr preserved; the caller decides
    whether to abort or attempt resolution. For the synthesizer's
    re-draft-on-current-main pattern, the usual response is:

        try:
            await rebase_onto(repo, "main")
        except GitError:
            await _run(repo, "rebase", "--abort", check=False)
            # fall back to recreating the branch from scratch

    That "recreate from scratch" path belongs in the caller, not here.
    """
    await _run(repo_path, "rebase", onto)


async def rebase_abort(repo_path: str | Path) -> None:
    """Abort an in-progress rebase. Safe to call even if none is running."""
    await _run(repo_path, "rebase", "--abort", check=False)


async def delete_branch(
    repo_path: str | Path,
    name: str,
    force: bool = False,
) -> None:
    """Delete a local branch. ``force=True`` passes ``-D`` for unmerged branches.

    The synthesizer does NOT call this — branch deletion is the subject's
    call during solo review when rejecting a draft. Kept here for test
    setup and potential future use.
    """
    flag = "-D" if force else "-d"
    await _run(repo_path, "branch", flag, name)


# --- Commit operations ---


async def add(repo_path: str | Path, *paths: str) -> None:
    """Stage specific paths. Prefer this over ``add_all`` to avoid sweeping
    up unrelated changes. Paths are relative to the repo root.
    """
    if not paths:
        raise ValueError("add() requires at least one path")
    await _run(repo_path, "add", "--", *paths)


async def commit(
    repo_path: str | Path,
    message: str,
    allow_empty: bool = False,
) -> str:
    """Create a commit with ``message``. Returns the new commit SHA.

    ``allow_empty=False`` (default) matches the subskill directive
    "Don't create empty commits." If there's nothing staged, this
    raises GitError.
    """
    args = ["commit", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    await _run(repo_path, *args)
    result = await _run(repo_path, "rev-parse", "HEAD")
    return result.stdout.strip()


# --- Diff / log ---


async def diff(
    repo_path: str | Path,
    base: str,
    head: str = "HEAD",
) -> str:
    """Return unified diff text between ``base`` and ``head``.

    Used by solo-review tooling (and by the synthesizer to see what's
    already on ``synth/draft`` before deciding whether to add more).
    """
    result = await _run(repo_path, "diff", f"{base}...{head}")
    return result.stdout


async def log_since(
    repo_path: str | Path,
    base: str,
    branch: str = "HEAD",
) -> list[str]:
    """Return one-line commit summaries of ``branch`` not in ``base``.

    Useful for inspecting what's accumulated on ``synth/draft`` across
    multiple synthesizer runs.
    """
    result = await _run(
        repo_path, "log", "--oneline", f"{base}..{branch}"
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


# --- Remote operations ---


async def has_remote(
    repo_path: str | Path,
    name: str = "origin",
) -> bool:
    """Whether the repo has a remote configured under the given name.

    Synthesizer sync becomes a no-op when no remote exists — useful for
    single-machine deployments where the subject repo isn't pushed
    anywhere.
    """
    result = await _run(
        repo_path, "remote", "get-url", name, check=False
    )
    return result.returncode == 0


async def fetch(
    repo_path: str | Path,
    remote: str = "origin",
    prune: bool = True,
) -> None:
    """Fetch from ``remote``. ``prune=True`` drops tracking refs whose
    upstream has been deleted — useful for ``synth/draft`` rejection
    propagation.
    """
    args = ["fetch", remote]
    if prune:
        args.append("--prune")
    await _run(repo_path, *args)


async def remote_branch_exists(
    repo_path: str | Path,
    name: str,
    remote: str = "origin",
) -> bool:
    """Whether ``remote/name`` exists as a remote-tracking ref locally.

    Requires a prior ``fetch`` to be current. Useful for deciding
    whether to sync ``synth/draft`` state at run start.
    """
    result = await _run(
        repo_path,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/{remote}/{name}",
        check=False,
    )
    return result.returncode == 0


async def reset_hard_to(
    repo_path: str | Path,
    ref: str,
) -> None:
    """Hard-reset the current branch to ``ref``.

    Destructive: discards any uncommitted changes and any commits on
    the current branch not reachable from ``ref``. Used by the
    synthesizer's run-start sync to bring local main in line with
    origin/main. The synthesizer owns main during its runs; anything
    uncommitted shouldn't be there.
    """
    await _run(repo_path, "reset", "--hard", ref)


async def push(
    repo_path: str | Path,
    branch: str,
    remote: str = "origin",
    force_with_lease: bool = False,
) -> None:
    """Push ``branch`` to ``remote``.

    ``force_with_lease=True`` is safe-forced push — replaces the remote
    branch only if the remote hasn't moved since we last fetched it.
    Used for ``synth/draft`` because the synthesizer freely rebases
    that branch each run. Never use ``force_with_lease`` on ``main`` —
    main is shared state; a genuine concurrent push to origin/main is
    a signal to back off, not clobber.
    """
    args = ["push", remote, branch]
    if force_with_lease:
        args.append("--force-with-lease")
    await _run(repo_path, *args)
