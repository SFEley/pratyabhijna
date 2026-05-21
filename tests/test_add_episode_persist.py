"""Tests for Stage 5 — graph writes.

Mock-based tests that verify which Cypher queries land and which save()
methods get awaited. Live end-to-end coverage comes via the parity tests
in test_add_episode_parity.py.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode, SagaNode

from pratyabhijna.add_episode.persist import (
    persist_edges_and_episode,
    persist_nodes_and_saga,
)


def _node(name="X", uuid="u-x", group="g"):
    n = MagicMock(spec=EntityNode)
    n.uuid = uuid
    n.name = name
    n.group_id = group
    n.attributes = {}
    n.save = AsyncMock()
    return n


def _edge(uuid="e", src="ua", tgt="ub"):
    e = MagicMock(spec=EntityEdge)
    e.uuid = uuid
    e.source_node_uuid = src
    e.target_node_uuid = tgt
    e.save = AsyncMock()
    return e


def _episode(uuid="ep", group="g"):
    ep = MagicMock(spec=EpisodicNode)
    ep.uuid = uuid
    ep.group_id = group
    ep.valid_at = datetime(2026, 5, 20, tzinfo=timezone.utc)
    ep.created_at = datetime(2026, 5, 20, tzinfo=timezone.utc)
    ep.save = AsyncMock()
    return ep


# ---------------------------------------------------------------------------
# Phase 5a
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_nodes_and_saga_saves_new_nodes():
    driver = AsyncMock()
    nodes = [_node("A", "u1"), _node("B", "u2")]
    result = await persist_nodes_and_saga(
        driver, new_nodes=nodes, updated_nodes=[], saga_name=None, group_id="g",
    )
    assert result is None
    for n in nodes:
        n.save.assert_awaited_once_with(driver)


@pytest.mark.asyncio
async def test_persist_nodes_and_saga_merges_attribute_updates():
    driver = AsyncMock()
    existing = _node("X", "u-x")
    existing.attributes = {"a": 1}
    await persist_nodes_and_saga(
        driver, new_nodes=[],
        updated_nodes=[(existing, {"b": 2})],
        saga_name=None, group_id="g",
    )
    assert existing.attributes == {"a": 1, "b": 2}
    existing.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_nodes_and_saga_creates_new_saga():
    driver = AsyncMock()
    # Saga lookup returns nothing → new saga is created.
    driver.execute_query = AsyncMock(return_value=([], None, None))
    # Patch SagaNode.save so we don't go further.
    with pytest.MonkeyPatch().context() as m:
        m.setattr(SagaNode, "save", AsyncMock())
        result = await persist_nodes_and_saga(
            driver, new_nodes=[], updated_nodes=[],
            saga_name="solo-sessions", group_id="g",
        )
    assert result is not None
    assert result.name == "solo-sessions"
    assert result.group_id == "g"


# ---------------------------------------------------------------------------
# Phase 5b
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_edges_writes_edges_and_episode_with_hash():
    driver = AsyncMock()
    driver.execute_query = AsyncMock()
    edges = [_edge("e1", "ua", "ub")]
    episode = _episode()
    await persist_edges_and_episode(
        driver,
        new_edges=edges, supersedes_uuids=[],
        episode=episode, episode_hash="deadbeef",
        touched_node_uuids=["ua", "ub"],
        saga=None, saga_prior_episode_uuid=None,
    )
    edges[0].save.assert_awaited_once_with(driver)
    episode.save.assert_awaited_once_with(driver)
    # episode_hash SET query was issued.
    queries = [c.args[0] for c in driver.execute_query.await_args_list]
    assert any("episode_hash" in q and "SET" in q for q in queries)


@pytest.mark.asyncio
async def test_persist_edges_invalidates_supersedes():
    driver = AsyncMock()
    driver.execute_query = AsyncMock()
    episode = _episode()
    await persist_edges_and_episode(
        driver,
        new_edges=[], supersedes_uuids=["old-e1", "old-e2"],
        episode=episode, episode_hash="h",
        touched_node_uuids=[],
        saga=None, saga_prior_episode_uuid=None,
    )
    queries = [c.args[0] for c in driver.execute_query.await_args_list]
    invalidate_queries = [q for q in queries if "invalid_at" in q and "SET" in q]
    assert invalidate_queries, "expected an invalid_at SET query"
    # The invalidate call had the expected uuids and bounds.
    super_call = next(
        c for c in driver.execute_query.await_args_list
        if "invalid_at" in c.args[0]
    )
    assert super_call.kwargs["uuids"] == ["old-e1", "old-e2"]
    assert super_call.kwargs["g"] == episode.group_id


@pytest.mark.asyncio
async def test_persist_creates_mentions_edges():
    driver = AsyncMock()
    driver.execute_query = AsyncMock()
    episode = _episode()
    await persist_edges_and_episode(
        driver,
        new_edges=[], supersedes_uuids=[],
        episode=episode, episode_hash="h",
        touched_node_uuids=["u1", "u2", "u3"],
        saga=None, saga_prior_episode_uuid=None,
    )
    queries = [c.args[0] for c in driver.execute_query.await_args_list]
    mentions_queries = [q for q in queries if "MENTIONS" in q]
    assert mentions_queries


@pytest.mark.asyncio
async def test_persist_saga_chain_adds_has_episode_and_next_episode():
    driver = AsyncMock()
    driver.execute_query = AsyncMock()
    episode = _episode()
    saga = MagicMock(spec=SagaNode)
    saga.uuid = "s1"
    await persist_edges_and_episode(
        driver,
        new_edges=[], supersedes_uuids=[],
        episode=episode, episode_hash="h",
        touched_node_uuids=[],
        saga=saga, saga_prior_episode_uuid="prev-ep",
    )
    queries = [c.args[0] for c in driver.execute_query.await_args_list]
    assert any("HAS_EPISODE" in q for q in queries)
    assert any("NEXT_EPISODE" in q for q in queries)


@pytest.mark.asyncio
async def test_persist_saga_without_prior_skips_next_episode():
    driver = AsyncMock()
    driver.execute_query = AsyncMock()
    episode = _episode()
    saga = MagicMock(spec=SagaNode)
    saga.uuid = "s1"
    await persist_edges_and_episode(
        driver,
        new_edges=[], supersedes_uuids=[],
        episode=episode, episode_hash="h",
        touched_node_uuids=[],
        saga=saga, saga_prior_episode_uuid=None,
    )
    queries = [c.args[0] for c in driver.execute_query.await_args_list]
    assert any("HAS_EPISODE" in q for q in queries)
    assert not any("NEXT_EPISODE" in q for q in queries)
