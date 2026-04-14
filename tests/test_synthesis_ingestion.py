"""Tests for the synthesizer's ingestion-candidate scan.

The scan walks the subject's repo and identifies files that need to go
through ``add_episode`` — either never ingested or edited since last
ingestion. Decisions about *which* candidates actually get ingested
live in the subskill's judgment, not here.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna.synthesis import (
    BOOTSTRAP_RELATIVE_PATHS,
    IngestionCandidate,
    scan_repo_for_ingestion_candidates,
)


# --- Helpers ---


def _write(path: Path, text: str, *, mtime: datetime | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))


def _episode(created_at: datetime) -> MagicMock:
    ep = MagicMock()
    ep.created_at = created_at
    return ep


def _service(episode_by_name: dict) -> MagicMock:
    svc = MagicMock()

    async def lookup(name):
        return episode_by_name.get(name)

    svc.get_latest_episode_by_name = AsyncMock(side_effect=lookup)
    return svc


# --- Basic scan semantics ---


@pytest.mark.asyncio
async def test_returns_empty_for_nonexistent_repo(tmp_path):
    svc = _service({})
    candidates = await scan_repo_for_ingestion_candidates(
        str(tmp_path / "nope"), svc
    )
    assert candidates == []


@pytest.mark.asyncio
async def test_returns_empty_for_empty_repo(tmp_path):
    svc = _service({})
    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)
    assert candidates == []


@pytest.mark.asyncio
async def test_new_file_becomes_new_candidate(tmp_path):
    _write(tmp_path / "writing" / "solo-1.md", "body")
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert len(candidates) == 1
    c = candidates[0]
    assert isinstance(c, IngestionCandidate)
    assert c.relative_path == "writing/solo-1.md"
    assert c.absolute_path == tmp_path / "writing" / "solo-1.md"
    assert c.reason == "new"
    assert c.latest_episode_at is None


@pytest.mark.asyncio
async def test_current_file_skipped(tmp_path):
    now = datetime.now(timezone.utc)
    file_mtime = now - timedelta(hours=2)
    _write(tmp_path / "writing" / "solo-2.md", "body", mtime=file_mtime)
    svc = _service({"writing/solo-2.md": _episode(now - timedelta(minutes=5))})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert candidates == []


@pytest.mark.asyncio
async def test_stale_file_becomes_stale_candidate(tmp_path):
    now = datetime.now(timezone.utc)
    episode_at = now - timedelta(days=2)
    file_mtime = now - timedelta(hours=1)  # edited after episode
    _write(tmp_path / "writing" / "solo-3.md", "revised", mtime=file_mtime)
    svc = _service({"writing/solo-3.md": _episode(episode_at)})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.relative_path == "writing/solo-3.md"
    assert c.reason == "stale"
    assert c.latest_episode_at == episode_at


# --- Exclusions ---


@pytest.mark.asyncio
async def test_bootstrap_files_excluded(tmp_path):
    for rel in BOOTSTRAP_RELATIVE_PATHS:
        _write(tmp_path / rel, "content")
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert candidates == []


@pytest.mark.asyncio
async def test_dot_directories_excluded(tmp_path):
    _write(tmp_path / ".git" / "HEAD", "ref: refs/heads/main")
    _write(tmp_path / ".cache" / "thing.txt", "junk")
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert candidates == []


@pytest.mark.asyncio
async def test_dot_files_excluded(tmp_path):
    _write(tmp_path / ".gitignore", "node_modules")
    _write(tmp_path / "writing" / ".DS_Store", "junk")
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert candidates == []


@pytest.mark.asyncio
async def test_non_bootstrap_file_in_memory_is_included(tmp_path):
    """A non-bootstrap file under memory/ is still a candidate.

    The exclusion is by specific path, not by directory. If Vesper
    keeps a notes file at memory/NOTES.md that isn't in the bootstrap
    set, it should be ingestible.
    """
    _write(tmp_path / "memory" / "NOTES.md", "extra notes")
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert len(candidates) == 1
    assert candidates[0].relative_path == "memory/NOTES.md"


@pytest.mark.asyncio
async def test_any_repo_location_considered(tmp_path):
    """Any non-bootstrap file is a candidate, not just writing/."""
    _write(tmp_path / "letters" / "first.md", "a")
    _write(tmp_path / "drafts" / "piece.md", "b")
    _write(tmp_path / "root-level.md", "c")
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    paths = {c.relative_path for c in candidates}
    assert paths == {"letters/first.md", "drafts/piece.md", "root-level.md"}


# --- Lookback window ---


@pytest.mark.asyncio
async def test_files_older_than_window_skipped(tmp_path):
    old_mtime = datetime.now(timezone.utc) - timedelta(days=30)
    _write(tmp_path / "writing" / "ancient.md", "old", mtime=old_mtime)
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(
        str(tmp_path), svc, max_age_days=14
    )

    assert candidates == []


@pytest.mark.asyncio
async def test_window_none_includes_ancient_files(tmp_path):
    old_mtime = datetime.now(timezone.utc) - timedelta(days=365)
    _write(tmp_path / "writing" / "ancient.md", "old", mtime=old_mtime)
    svc = _service({})

    candidates = await scan_repo_for_ingestion_candidates(
        str(tmp_path), svc, max_age_days=None
    )

    assert len(candidates) == 1


@pytest.mark.asyncio
async def test_stale_within_window_still_returned(tmp_path):
    """A file edited recently with a much older episode is still 'stale'."""
    now = datetime.now(timezone.utc)
    file_mtime = now - timedelta(hours=6)
    episode_at = now - timedelta(days=30)
    _write(tmp_path / "writing" / "revised.md", "v2", mtime=file_mtime)
    svc = _service({"writing/revised.md": _episode(episode_at)})

    candidates = await scan_repo_for_ingestion_candidates(
        str(tmp_path), svc, max_age_days=14
    )

    assert len(candidates) == 1
    assert candidates[0].reason == "stale"


# --- Episode timestamp handling ---


@pytest.mark.asyncio
async def test_naive_episode_timestamp_treated_as_utc(tmp_path):
    """Graphiti sometimes returns naive datetimes; treat them as UTC."""
    now = datetime.now(timezone.utc)
    file_mtime = now - timedelta(hours=1)
    # Naive datetime in the future relative to file — should be treated
    # as UTC and the file should be current (not stale).
    naive_episode_at = now.replace(tzinfo=None)
    _write(tmp_path / "writing" / "x.md", "x", mtime=file_mtime)
    svc = _service({"writing/x.md": _episode(naive_episode_at)})

    candidates = await scan_repo_for_ingestion_candidates(str(tmp_path), svc)

    assert candidates == []
