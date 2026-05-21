"""Pipeline orchestrator tests.

Each per-stage module gets its own focused test module; this file covers
the orchestrator's stage-stitching and the Stage 0 idempotency gate.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphiti_core.nodes import EpisodeType, EpisodicNode


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


# ---------------------------------------------------------------------------
# Stage 1: pre-flight
# ---------------------------------------------------------------------------


def _ts(d: int = 20):
    return datetime(2026, 5, d, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_prefetch_loads_previous_episodes():
    from pratyabhijna.add_episode.pipeline import _prefetch

    fake_episodes = [MagicMock(spec=EpisodicNode, uuid=f"u{i}") for i in range(5)]
    graphiti = MagicMock()
    graphiti.retrieve_episodes = AsyncMock(return_value=fake_episodes)
    graphiti.driver = AsyncMock()

    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=_ts(),
        source=EpisodeType.message,
        previous_n=5,
        saga=None,
        saga_previous_episode_uuid=None,
    )
    assert len(result.previous_episodes) == 5
    assert result.saga_prior_uuid is None
    graphiti.retrieve_episodes.assert_awaited_once()


@pytest.mark.asyncio
async def test_prefetch_resolves_saga_prior_when_uuid_not_given():
    from pratyabhijna.add_episode.pipeline import _prefetch

    graphiti = MagicMock()
    graphiti.retrieve_episodes = AsyncMock(return_value=[])
    graphiti.driver = AsyncMock()
    graphiti.driver.execute_query = AsyncMock(return_value=(
        [{"uuid": "prior-saga-ep"}], None, None,
    ))

    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=_ts(),
        source=EpisodeType.message,
        previous_n=5,
        saga="solo-sessions",
        saga_previous_episode_uuid=None,
    )
    assert result.saga_prior_uuid == "prior-saga-ep"


@pytest.mark.asyncio
async def test_prefetch_uses_explicit_saga_uuid_without_query():
    """When the caller passes saga_previous_episode_uuid, skip the saga lookup."""
    from pratyabhijna.add_episode.pipeline import _prefetch

    graphiti = MagicMock()
    graphiti.retrieve_episodes = AsyncMock(return_value=[])
    graphiti.driver = AsyncMock()
    graphiti.driver.execute_query = AsyncMock()

    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=_ts(),
        source=EpisodeType.message,
        previous_n=5,
        saga="solo-sessions",
        saga_previous_episode_uuid="explicit-uuid",
    )
    assert result.saga_prior_uuid == "explicit-uuid"
    graphiti.driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_prefetch_no_saga_no_lookup():
    from pratyabhijna.add_episode.pipeline import _prefetch

    graphiti = MagicMock()
    graphiti.retrieve_episodes = AsyncMock(return_value=[])
    graphiti.driver = AsyncMock()
    graphiti.driver.execute_query = AsyncMock()

    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=_ts(),
        source=EpisodeType.message,
        previous_n=5,
        saga=None,
        saga_previous_episode_uuid=None,
    )
    assert result.saga_prior_uuid is None
    graphiti.driver.execute_query.assert_not_awaited()
