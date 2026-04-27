"""Tests for the `inspect` MCP tool.

TDD: these tests define the inspect tool contract. They should fail
until inspect.py is implemented and wired into the server.

The inspect tool takes a UUID and returns full detail — either an
entity node (with connected edges and episodes) or an edge (with
resolved source/target entities and episode provenance).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError

from helpers import make_entity_edge, make_entity_node, make_episodic_node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService with entity/edge lookup methods."""
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_uuid = AsyncMock()
    service.get_edge = AsyncMock()
    service.get_edges_for_node = AsyncMock()
    service.get_episodes_for_node = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# Entity inspection
# ---------------------------------------------------------------------------

class TestInspectEntity:
    async def test_inspect_entity(self, mock_service):
        """UUID resolving to a node returns full entity detail."""
        from pratyabhijna.tools.inspect import inspect

        entity = make_entity_node(
            uuid="node-1",
            name="Serah",
            labels=["Person"],
            summary="Host alter, software engineer",
            attributes={"gender": "female", "person_type": "alter"},
        )
        edges = [
            make_entity_edge(
                uuid="edge-1",
                fact="Serah values directness",
                source_node_uuid="node-1",
                target_node_uuid="node-2",
            ),
        ]
        episodes = [
            make_episodic_node(uuid="ep-1", content="Serah said she values directness."),
        ]
        mock_service.get_entity_by_uuid.return_value = entity
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_episodes_for_node.return_value = episodes

        result = await inspect(service=mock_service, uuid="node-1")

        assert result["type"] == "entity"
        assert result["uuid"] == "node-1"
        assert result["name"] == "Serah"
        assert result["labels"] == ["Person"]
        assert result["summary"] == "Host alter, software engineer"
        assert result["attributes"]["gender"] == "female"
        assert isinstance(result["edges"], list)
        assert len(result["edges"]) == 1
        assert isinstance(result["episodes"], list)
        assert len(result["episodes"]) == 1

    async def test_inspect_entity_edges_show_direction(self, mock_service):
        """Connected edges are marked as outgoing or incoming."""
        from pratyabhijna.tools.inspect import inspect

        entity = make_entity_node(uuid="node-1", name="Serah")
        edges = [
            make_entity_edge(
                uuid="edge-out",
                fact="Serah values directness",
                source_node_uuid="node-1",
                target_node_uuid="node-2",
            ),
            make_entity_edge(
                uuid="edge-in",
                fact="Vesper was created by Serah",
                source_node_uuid="node-3",
                target_node_uuid="node-1",
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = entity
        mock_service.get_edges_for_node.return_value = edges
        mock_service.get_episodes_for_node.return_value = []

        result = await inspect(service=mock_service, uuid="node-1")

        edge_out = [e for e in result["edges"] if e["uuid"] == "edge-out"][0]
        edge_in = [e for e in result["edges"] if e["uuid"] == "edge-in"][0]
        assert edge_out["direction"] == "outgoing"
        assert edge_in["direction"] == "incoming"


# ---------------------------------------------------------------------------
# Edge inspection
# ---------------------------------------------------------------------------

class TestInspectEdge:
    async def test_inspect_edge(self, mock_service):
        """UUID resolving to an edge returns full edge detail."""
        from pratyabhijna.tools.inspect import inspect

        # get_entity_by_uuid raises NodeNotFoundError — it's an edge, not a node
        mock_service.get_entity_by_uuid.side_effect = NodeNotFoundError("edge-1")
        edge = make_entity_edge(
            uuid="edge-1",
            name="values",
            fact="Serah values directness",
            source_node_uuid="node-1",
            target_node_uuid="node-2",
            valid_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
            episodes=["ep-1"],
        )
        mock_service.get_edge.return_value = edge

        source_node = make_entity_node(uuid="node-1", name="Serah", labels=["Person"])
        target_node = make_entity_node(uuid="node-2", name="directness", labels=["Observation"])
        # After the first call raises, reset side_effect for entity resolution
        call_count = 0
        async def get_entity_dispatch(uuid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NodeNotFoundError(uuid)
            return {"node-1": source_node, "node-2": target_node}[uuid]
        mock_service.get_entity_by_uuid = AsyncMock(side_effect=get_entity_dispatch)

        episode = make_episodic_node(uuid="ep-1", content="Serah said she values directness.")
        mock_service.get_episodes_by_uuids = AsyncMock(return_value=[episode])

        result = await inspect(service=mock_service, uuid="edge-1")

        assert result["type"] == "edge"
        assert result["uuid"] == "edge-1"
        assert result["name"] == "values"
        assert result["fact"] == "Serah values directness"
        assert result["valid_at"] is not None
        assert result["invalid_at"] is None

    async def test_inspect_edge_resolves_entities(self, mock_service):
        """Edge source/target UUIDs are resolved to entity names and labels."""
        from pratyabhijna.tools.inspect import inspect

        mock_service.get_entity_by_uuid.side_effect = NodeNotFoundError("edge-1")
        edge = make_entity_edge(
            uuid="edge-1",
            source_node_uuid="node-1",
            target_node_uuid="node-2",
        )
        mock_service.get_edge.return_value = edge

        source_node = make_entity_node(uuid="node-1", name="Serah", labels=["Person"])
        target_node = make_entity_node(uuid="node-2", name="directness", labels=["Observation"])
        call_count = 0
        async def get_entity_dispatch(uuid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NodeNotFoundError(uuid)
            return {"node-1": source_node, "node-2": target_node}[uuid]
        mock_service.get_entity_by_uuid = AsyncMock(side_effect=get_entity_dispatch)
        mock_service.get_episodes_by_uuids = AsyncMock(return_value=[])

        result = await inspect(service=mock_service, uuid="edge-1")

        assert result["source_entity"]["name"] == "Serah"
        assert result["source_entity"]["labels"] == ["Person"]
        assert result["target_entity"]["name"] == "directness"
        assert result["target_entity"]["labels"] == ["Observation"]

    async def test_inspect_edge_with_episodes(self, mock_service):
        """Edge episode UUIDs are fetched and returned with content."""
        from pratyabhijna.tools.inspect import inspect

        mock_service.get_entity_by_uuid.side_effect = NodeNotFoundError("edge-1")
        edge = make_entity_edge(
            uuid="edge-1",
            episodes=["ep-1", "ep-2"],
        )
        mock_service.get_edge.return_value = edge

        source_node = make_entity_node(uuid="node-1", name="Serah")
        target_node = make_entity_node(uuid="node-2", name="directness")
        call_count = 0
        async def get_entity_dispatch(uuid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NodeNotFoundError(uuid)
            return {"node-1": source_node, "node-2": target_node}[uuid]
        mock_service.get_entity_by_uuid = AsyncMock(side_effect=get_entity_dispatch)

        episodes = [
            make_episodic_node(uuid="ep-1", content="First conversation"),
            make_episodic_node(uuid="ep-2", content="Second conversation"),
        ]
        mock_service.get_episodes_by_uuids = AsyncMock(return_value=episodes)

        result = await inspect(service=mock_service, uuid="edge-1")

        assert len(result["episodes"]) == 2
        contents = {e["content"] for e in result["episodes"]}
        assert "First conversation" in contents
        assert "Second conversation" in contents


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------

class TestInspectNotFound:
    async def test_inspect_not_found(self, mock_service):
        """UUID matching neither node nor edge returns error dict."""
        from pratyabhijna.tools.inspect import inspect

        mock_service.get_entity_by_uuid.side_effect = NodeNotFoundError("unknown-uuid")
        mock_service.get_edge.side_effect = EdgeNotFoundError("unknown-uuid")

        result = await inspect(service=mock_service, uuid="unknown-uuid")

        assert result["error"] == "not_found"
        assert result["uuid"] == "unknown-uuid"
        assert "message" in result
