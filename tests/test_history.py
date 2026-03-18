"""Tests for the `history` MCP tool.

TDD: these tests define the history tool contract. They should fail
until history.py is implemented and wired into the server.

The history tool finds an entity by name (or UUID) and returns a
chronological timeline of all edges — showing what was believed when,
and what superseded what.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity_node(
    uuid="node-1",
    name="Serah",
    labels=None,
    summary="A person",
    attributes=None,
    created_at=None,
):
    return EntityNode(
        uuid=uuid,
        name=name,
        group_id="default",
        labels=labels or ["Person"],
        created_at=created_at or datetime(2026, 3, 15, tzinfo=timezone.utc),
        name_embedding=None,
        summary=summary,
        attributes=attributes or {},
    )


def _make_entity_edge(
    uuid="edge-1",
    name="lives_in",
    fact="Serah lives in Atlanta",
    source_node_uuid="node-1",
    target_node_uuid="node-2",
    valid_at=None,
    invalid_at=None,
    created_at=None,
    episodes=None,
    attributes=None,
):
    return EntityEdge(
        uuid=uuid,
        group_id="default",
        source_node_uuid=source_node_uuid,
        target_node_uuid=target_node_uuid,
        created_at=created_at or datetime(2026, 3, 15, tzinfo=timezone.utc),
        name=name,
        fact=fact,
        fact_embedding=None,
        episodes=episodes or [],
        expired_at=None,
        valid_at=valid_at,
        invalid_at=invalid_at,
        attributes=attributes or {},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock VesperService with entity lookup and edge retrieval methods."""
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_name = AsyncMock()
    service.get_edges_for_node = AsyncMock()
    service.get_entity = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

class TestHistoryTimeline:
    async def test_history_returns_timeline(self, mock_service):
        """history() returns entity info and a chronological timeline of edges."""
        from vesper.tools.history import history

        entity = _make_entity_node(uuid="node-1", name="Serah")
        edges = [
            _make_entity_edge(
                uuid="edge-1",
                fact="Serah joined the project",
                valid_at=datetime(2025, 10, 1, tzinfo=timezone.utc),
                created_at=datetime(2025, 10, 1, tzinfo=timezone.utc),
            ),
            _make_entity_edge(
                uuid="edge-2",
                fact="Serah started Phase 2",
                valid_at=datetime(2026, 3, 13, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 13, tzinfo=timezone.utc),
            ),
            _make_entity_edge(
                uuid="edge-3",
                fact="Serah completed Phase 3",
                valid_at=datetime(2026, 3, 17, tzinfo=timezone.utc),
                created_at=datetime(2026, 3, 17, tzinfo=timezone.utc),
            ),
        ]
        mock_service.get_entity_by_name.return_value = entity
        mock_service.get_edges_for_node.return_value = edges

        result = await history(service=mock_service, entity_name="Serah")

        assert result["entity"]["uuid"] == "node-1"
        assert result["entity"]["name"] == "Serah"
        assert result["entity"]["labels"] == ["Person"]
        assert result["count"] == 3
        assert len(result["timeline"]) == 3
        # Each timeline entry should have essential fields
        entry = result["timeline"][0]
        assert "uuid" in entry
        assert "fact" in entry
        assert "valid_at" in entry
        assert "invalid_at" in entry
        assert "created_at" in entry

    async def test_history_shows_supersession(self, mock_service):
        """Superseded edges show invalid_at; current edges show None."""
        from vesper.tools.history import history

        entity = _make_entity_node(uuid="node-1", name="Serah")
        edges = [
            _make_entity_edge(
                uuid="edge-old",
                fact="Serah lives in Atlanta",
                valid_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                invalid_at=datetime(2025, 8, 1, tzinfo=timezone.utc),
            ),
            _make_entity_edge(
                uuid="edge-current",
                fact="Serah lives in Aurora, CO",
                valid_at=datetime(2025, 8, 1, tzinfo=timezone.utc),
                invalid_at=None,
            ),
        ]
        mock_service.get_entity_by_name.return_value = entity
        mock_service.get_edges_for_node.return_value = edges

        result = await history(service=mock_service, entity_name="Serah")

        timeline = result["timeline"]
        assert len(timeline) == 2
        # Old edge should have invalid_at set
        old = [e for e in timeline if e["uuid"] == "edge-old"][0]
        assert old["invalid_at"] is not None
        # Current edge should have invalid_at as None
        current = [e for e in timeline if e["uuid"] == "edge-current"][0]
        assert current["invalid_at"] is None

    async def test_history_sorts_by_valid_at(self, mock_service):
        """Timeline is sorted chronologically by valid_at, falling back to created_at."""
        from vesper.tools.history import history

        entity = _make_entity_node(uuid="node-1", name="Serah")
        # Return edges out of chronological order
        edges = [
            _make_entity_edge(
                uuid="edge-late",
                fact="late event",
                valid_at=datetime(2026, 3, 17, tzinfo=timezone.utc),
            ),
            _make_entity_edge(
                uuid="edge-early",
                fact="early event",
                valid_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            ),
            _make_entity_edge(
                uuid="edge-middle",
                fact="middle event, no valid_at",
                valid_at=None,
                created_at=datetime(2025, 6, 15, tzinfo=timezone.utc),
            ),
        ]
        mock_service.get_entity_by_name.return_value = entity
        mock_service.get_edges_for_node.return_value = edges

        result = await history(service=mock_service, entity_name="Serah")

        uuids = [e["uuid"] for e in result["timeline"]]
        assert uuids == ["edge-early", "edge-middle", "edge-late"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestHistoryEdgeCases:
    async def test_history_entity_not_found(self, mock_service):
        """Entity not found returns error dict, not an exception."""
        from vesper.tools.history import history

        mock_service.get_entity_by_name.return_value = None

        result = await history(service=mock_service, entity_name="Nonexistent")

        assert result["error"] == "not_found"
        assert "Nonexistent" in result["message"]

    async def test_history_no_edges(self, mock_service):
        """Entity with no edges returns empty timeline."""
        from vesper.tools.history import history

        entity = _make_entity_node(uuid="node-1", name="Serah")
        mock_service.get_entity_by_name.return_value = entity
        mock_service.get_edges_for_node.return_value = []

        result = await history(service=mock_service, entity_name="Serah")

        assert result["entity"]["name"] == "Serah"
        assert result["timeline"] == []
        assert result["count"] == 0
