"""Pipeline orchestrator tests.

Each per-stage module gets its own focused test module; this file covers
the orchestrator's stage-stitching and the Stage 0 idempotency gate.
"""

from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Stage 0: idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_hit_returns_existing_uuid():
    from pratyabhijna.add_episode.pipeline import _check_idempotency

    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=(
        [{"uuid": "existing-123"}], None, None,
    ))
    hit = await _check_idempotency(driver, group_id="vesper", episode_hash="deadbeef")
    assert hit == "existing-123"


@pytest.mark.asyncio
async def test_idempotency_miss_returns_none():
    from pratyabhijna.add_episode.pipeline import _check_idempotency

    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=([], None, None))
    hit = await _check_idempotency(driver, group_id="vesper", episode_hash="newhash")
    assert hit is None


@pytest.mark.asyncio
async def test_idempotency_query_scopes_to_group_and_hash():
    """Verify the Cypher MATCH includes both the group_id and the hash."""
    from pratyabhijna.add_episode.pipeline import _check_idempotency

    driver = AsyncMock()
    driver.execute_query = AsyncMock(return_value=([], None, None))
    await _check_idempotency(driver, group_id="vesper", episode_hash="abc")
    args = driver.execute_query.await_args
    query = args.args[0]
    assert "group_id" in query
    assert "episode_hash" in query
    assert args.kwargs["group_id"] == "vesper"
    assert args.kwargs["episode_hash"] == "abc"
