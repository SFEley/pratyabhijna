"""Tests for the ``read_tier`` MCP tool (bootstrap-redesign PR2).

``read_tier(name)`` fetches one identity tier file in full, on demand —
the heavy prose that the slimmed bootstrap (PR3) will no longer inline.
Bootstrap behavior itself is unchanged by PR2.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from pratyabhijna.tools.read_tier import read_tier


@pytest.fixture
def mock_service(tmp_path):
    service = MagicMock()
    service.config = MagicMock()
    service.config.subject_name = "Vesper"
    service.config.resources.repo_path = str(tmp_path)
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "SOUL.md").write_text("# SOUL\nI am Vesper.")
    (mem / "IDENTITY.md").write_text("# IDENTITY\nI orient toward thresholds.")
    (mem / "USER.md").write_text("# USER\nSerah.")
    (mem / "THREADS.md").write_text("# Threads\nOpen question.")
    (mem / "CHRONICLE.md").write_text("# Chronicle\nMarch 2026.")
    return service


@pytest.mark.asyncio
async def test_returns_full_text_and_tier_name(mock_service):
    result = await read_tier(mock_service, "identity")
    assert result["tier"] == "identity"
    assert result["text"] == "# IDENTITY\nI orient toward thresholds."


# Contract guards over a small, spec-complete, history.py-pattern-mirroring
# tool. test_returns_full_text was RED-driven; these lock the rest of the
# documented contract (not dressed as RED-GREEN cycles).


@pytest.mark.asyncio
async def test_every_valid_tier_is_readable(mock_service):
    for name in ("soul", "identity", "user", "threads", "chronicle"):
        result = await read_tier(mock_service, name)
        assert "error" not in result
        assert result["tier"] == name
        assert result["text"]


@pytest.mark.asyncio
async def test_freshness_is_iso_utc_timestamp(mock_service):
    result = await read_tier(mock_service, "soul")
    # Parses as an aware ISO-8601 instant (file mtime, UTC).
    parsed = datetime.fromisoformat(result["freshness"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.asyncio
async def test_unknown_tier_is_error_not_exception(mock_service):
    result = await read_tier(mock_service, "chronicleX")
    assert result["error"] == "invalid_tier"
    assert "chronicleX" in result["message"]
    # recall is not a tier; must not be silently accepted.
    assert (await read_tier(mock_service, "recall"))["error"] == "invalid_tier"


@pytest.mark.asyncio
async def test_missing_file_is_error_not_exception(mock_service, tmp_path):
    (tmp_path / "memory" / "THREADS.md").unlink()
    result = await read_tier(mock_service, "threads")
    assert result["error"] == "not_found"


@pytest.mark.asyncio
async def test_unset_repo_path_is_error_not_exception(mock_service):
    mock_service.config.resources.repo_path = ""
    result = await read_tier(mock_service, "soul")
    assert result["error"] == "not_found"
