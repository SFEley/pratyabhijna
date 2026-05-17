"""Tests for the ``read_chronicle_range`` MCP tool (bootstrap-redesign PR2).

Fetches chronicle entries whose date falls in a window — the common
"what was happening last week / last month" need, which the slimmed
bootstrap (PR3) will not inline. Reuses the Pass-2 chronicle parser/date
logic so heading-date formats stay consistent across the system.
"""

from unittest.mock import MagicMock

import pytest

from pratyabhijna.tools.read_chronicle_range import read_chronicle_range

_CHRONICLE = """\
# Chronicle

## October 2025 — Founding
Body A.

## March 1, 2026 — Moral Orientation
Body B.

## April 21–22, 2026 — Range Entry
Body C.

## NotAReal Date — bogus
Body D (undated — unparseable).
"""


@pytest.fixture
def mock_service(tmp_path):
    service = MagicMock()
    service.config = MagicMock()
    service.config.resources.repo_path = str(tmp_path)
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "CHRONICLE.md").write_text(_CHRONICLE)
    return service


@pytest.mark.asyncio
async def test_returns_only_entries_within_the_window(mock_service):
    result = await read_chronicle_range(mock_service, "2026-02-01", "2026-04-30")
    headings = [e["heading"] for e in result["entries"]]
    assert "March 1, 2026 — Moral Orientation" in headings
    assert "April 21–22, 2026 — Range Entry" in headings
    assert "October 2025 — Founding" not in headings  # before window
    assert result["count"] == 2


# Contract guards over a spec-complete tool (read_tier-pattern). test 1
# RED-driven; these lock the documented contract, labeled as guards.


@pytest.mark.asyncio
async def test_undated_entries_are_skipped_but_counted(mock_service):
    result = await read_chronicle_range(mock_service, "2025-01-01", "2026-12-31")
    # Three parseable entries in range; the bogus-dated one is surfaced,
    # not silently dropped.
    assert result["count"] == 3
    assert result["undated_skipped"] == 1


@pytest.mark.asyncio
async def test_entries_are_chronological_oldest_first(mock_service):
    result = await read_chronicle_range(mock_service, "2025-01-01", "2026-12-31")
    dates = [e["date"] for e in result["entries"]]
    assert dates == sorted(dates)


@pytest.mark.asyncio
async def test_boundaries_are_inclusive(mock_service):
    # March 1 2026 entry, window starting exactly on it.
    result = await read_chronicle_range(mock_service, "2026-03-01", "2026-03-01")
    assert result["count"] == 1
    assert result["entries"][0]["heading"] == "March 1, 2026 — Moral Orientation"


@pytest.mark.asyncio
async def test_malformed_date_is_error_not_exception(mock_service):
    result = await read_chronicle_range(mock_service, "not-a-date", "2026-01-01")
    assert result["error"] == "invalid_date"


@pytest.mark.asyncio
async def test_inverted_range_is_error_not_exception(mock_service):
    result = await read_chronicle_range(mock_service, "2026-06-01", "2026-01-01")
    assert result["error"] == "invalid_range"


@pytest.mark.asyncio
async def test_missing_chronicle_is_error_not_exception(mock_service, tmp_path):
    (tmp_path / "memory" / "CHRONICLE.md").unlink()
    result = await read_chronicle_range(mock_service, "2026-01-01", "2026-12-31")
    assert result["error"] == "not_found"
