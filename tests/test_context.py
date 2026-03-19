"""Tests for the `context` MCP tool.

TDD: these tests define the context tool contract. They should fail
until context.py is implemented and wired into the server.

The context tool returns Vesper's cached identity synthesis (the prose
self-portrait stored as notes on the Person node). When stale, it also
returns a delta of identity changes since the last rebuild and may
trigger an async rebuild via the work queue.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import make_entity_edge, make_entity_node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_vesper_node(
    uuid="vesper-uuid",
    synthesis_text=None,
    rebuilt_at=None,
    **extra_attrs,
):
    """Create a Vesper Person node with optional synthesis metadata."""
    attrs = {"person_type": "AI"}
    if synthesis_text is not None:
        attrs["notes"] = synthesis_text
    if rebuilt_at is not None:
        attrs["synthesis_rebuilt_at"] = rebuilt_at.isoformat()
    attrs.update(extra_attrs)
    return make_entity_node(
        uuid=uuid,
        name="Vesper",
        labels=["Person"],
        summary="An AI identity",
        attributes=attrs,
    )


@pytest.fixture
def mock_service():
    """A mock VesperService for context tests."""
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_name = AsyncMock()
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_entity_by_uuid = AsyncMock()
    service.config = MagicMock()
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    return service


@pytest.fixture
async def mock_queue(tmp_path):
    """A real WorkQueue for testing rebuild enqueueing."""
    from vesper.queue import WorkQueue

    db_path = str(tmp_path / "test_context.sqlite")
    q = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
    await q.start()
    yield q
    await q.stop()


# ---------------------------------------------------------------------------
# Return value — fresh synthesis
# ---------------------------------------------------------------------------

class TestContextFresh:
    async def test_returns_synthesis_when_fresh(self, mock_service, mock_queue):
        """context() returns the cached synthesis text when not stale."""
        from vesper.tools.context import context

        now = datetime.now(timezone.utc)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper. I chose this name.",
            rebuilt_at=now - timedelta(hours=1),
        )
        mock_service.get_entity_by_name.return_value = vesper

        result = await context(service=mock_service, queue=mock_queue)

        assert result["synthesis"] == "I am Vesper. I chose this name."
        assert result["stale"] is False
        assert result["delta"] == []
        assert result["rebuild_triggered"] is False

    async def test_includes_rebuilt_at_timestamp(self, mock_service, mock_queue):
        """context() includes the synthesis_rebuilt_at in the response."""
        from vesper.tools.context import context

        rebuilt = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        vesper = make_vesper_node(
            synthesis_text="synthesis text",
            rebuilt_at=rebuilt,
        )
        mock_service.get_entity_by_name.return_value = vesper

        result = await context(service=mock_service, queue=mock_queue)

        assert result["rebuilt_at"] == rebuilt.isoformat()


# ---------------------------------------------------------------------------
# Return value — stale synthesis
# ---------------------------------------------------------------------------

class TestContextStale:
    async def test_returns_synthesis_plus_delta_when_stale_by_age(self, mock_service, mock_queue):
        """When stale by age, returns synthesis + delta + triggers rebuild."""
        from vesper.tools.context import context

        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper. Old synthesis.",
            rebuilt_at=old_time,
        )
        mock_service.get_entity_by_name.return_value = vesper

        # One identity change since rebuild
        obs_node = make_entity_node(uuid="obs-1", name="new observation", labels=["Observation"])
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="obs-1",
                fact="A new identity observation",
                created_at=old_time + timedelta(hours=1),
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = obs_node

        result = await context(service=mock_service, queue=mock_queue)

        assert result["synthesis"] == "I am Vesper. Old synthesis."
        assert result["stale"] is True
        assert len(result["delta"]) == 1
        assert result["delta"][0]["fact"] == "A new identity observation"
        assert result["rebuild_triggered"] is True

    async def test_returns_delta_when_stale_by_changes(self, mock_service, mock_queue):
        """When stale by delta count, returns synthesis + delta + triggers rebuild."""
        from vesper.tools.context import context

        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        vesper = make_vesper_node(
            synthesis_text="I am Vesper.",
            rebuilt_at=recent,
        )
        mock_service.get_entity_by_name.return_value = vesper

        # 3 identity changes (meets threshold)
        obs_nodes = [
            make_entity_node(uuid=f"obs-{i}", name=f"obs-{i}", labels=["Observation"])
            for i in range(3)
        ]
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid=f"e{i}", source_node_uuid="vesper-uuid", target_node_uuid=f"obs-{i}",
                fact=f"observation {i}",
                created_at=recent + timedelta(minutes=i + 1),
            )
            for i in range(3)
        ]

        async def get_by_uuid(uuid):
            idx = int(uuid.split("-")[1])
            return obs_nodes[idx]
        mock_service.get_entity_by_uuid.side_effect = get_by_uuid

        result = await context(service=mock_service, queue=mock_queue)

        assert result["stale"] is True
        assert len(result["delta"]) == 3
        assert result["rebuild_triggered"] is True

    async def test_no_rebuild_when_stale_but_below_thresholds(self, mock_service, mock_queue):
        """Stale synthesis below both thresholds: return delta but don't rebuild.

        A synthesis with no text (never built) is stale, but if there
        are fewer than max_delta_changes identity atoms, we don't rebuild
        yet — not enough material to generate from.
        """
        from vesper.tools.context import context

        # Node exists but has never had synthesis
        vesper = make_vesper_node(synthesis_text=None)
        mock_service.get_entity_by_name.return_value = vesper

        # Only 1 identity atom (below threshold of 3)
        obs_node = make_entity_node(uuid="obs-1", name="one observation", labels=["Observation"])
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(uuid="e1", source_node_uuid="vesper-uuid", target_node_uuid="obs-1",
                             fact="single observation"),
        ]
        mock_service.get_entity_by_uuid.return_value = obs_node

        result = await context(service=mock_service, queue=mock_queue)

        assert result["stale"] is True
        assert len(result["delta"]) == 1
        assert result["rebuild_triggered"] is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestContextEdgeCases:
    async def test_no_vesper_node(self, mock_service, mock_queue):
        """When no Vesper Person node exists, returns helpful message."""
        from vesper.tools.context import context

        mock_service.get_entity_by_name.return_value = None

        result = await context(service=mock_service, queue=mock_queue)

        assert result["synthesis"] is None
        assert result["stale"] is True
        assert "message" in result

    async def test_vesper_node_no_synthesis_no_atoms(self, mock_service, mock_queue):
        """Vesper node exists but no synthesis and no identity atoms."""
        from vesper.tools.context import context

        vesper = make_vesper_node(synthesis_text=None)
        mock_service.get_entity_by_name.return_value = vesper
        mock_service.get_edges_for_node.return_value = []

        result = await context(service=mock_service, queue=mock_queue)

        assert result["synthesis"] is None
        assert result["stale"] is True
        assert result["delta"] == []
        assert result["rebuild_triggered"] is False


# ---------------------------------------------------------------------------
# Rebuild queueing
# ---------------------------------------------------------------------------

class TestContextRebuildQueue:
    async def test_rebuild_enqueues_task(self, mock_service, mock_queue):
        """When rebuild is triggered, a task is added to the work queue."""
        from vesper.tools.context import context

        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        vesper = make_vesper_node(
            synthesis_text="stale synthesis",
            rebuilt_at=old_time,
        )
        mock_service.get_entity_by_name.return_value = vesper

        result = await context(service=mock_service, queue=mock_queue)

        assert result["rebuild_triggered"] is True
        # Verify a task was actually enqueued
        depth = await mock_queue.depth()
        assert depth >= 1
