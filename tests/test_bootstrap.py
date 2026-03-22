"""Tests for the `bootstrap` MCP tool.

TDD: these tests define the bootstrap tool contract. They should fail
until bootstrap.py is implemented and wired into the server.

The bootstrap tool returns the cached identity synthesis (prose
self-portrait stored as notes on the subject Person node) plus a
delta of identity changes since the last rebuild. It is a pure read
with no side effects — synthesis rebuilds are triggered by write
handlers, not by this tool.
"""

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService for bootstrap tests."""
    service = MagicMock()
    service.is_connected = True
    service.get_entity_by_name = AsyncMock()
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_entity_by_uuid = AsyncMock()
    service.config = MagicMock()
    service.config.subject_name = "Vesper"
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    return service


# ---------------------------------------------------------------------------
# Return value — normal operation
# ---------------------------------------------------------------------------

class TestBootstrapReturn:
    async def test_returns_synthesis_text(self, mock_service):
        """bootstrap() returns the cached synthesis text from the subject node."""
        from pratyabhijna.tools.bootstrap import bootstrap

        now = datetime.now(timezone.utc)
        node = make_subject_node(
            synthesis_text="I am Vesper. I chose this name.",
            rebuilt_at=now - timedelta(hours=1),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["synthesis"] == "I am Vesper. I chose this name."

    async def test_returns_rebuilt_at_timestamp(self, mock_service):
        """bootstrap() includes the synthesis_rebuilt_at in the response."""
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuilt = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(
            synthesis_text="synthesis text",
            rebuilt_at=rebuilt,
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["rebuilt_at"] == rebuilt.isoformat()

    async def test_returns_subject_name(self, mock_service):
        """bootstrap() includes the subject name in the response."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(
            synthesis_text="synthesis text",
            rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["subject"] == "Vesper"

    async def test_uses_configured_subject_name(self, mock_service):
        """bootstrap() uses config.subject_name, not a hardcoded name."""
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.config.subject_name = "Aria"
        node = make_subject_node(
            name="Aria",
            uuid="aria-uuid",
            synthesis_text="I am Aria.",
            rebuilt_at=datetime.now(timezone.utc),
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        mock_service.get_entity_by_name.assert_called_once_with("Aria")
        assert result["subject"] == "Aria"
        assert result["synthesis"] == "I am Aria."

    async def test_returns_delta(self, mock_service):
        """bootstrap() returns identity atoms created since the last rebuild."""
        from pratyabhijna.tools.bootstrap import bootstrap

        rebuild_time = datetime(2026, 3, 18, 12, 0, tzinfo=timezone.utc)
        node = make_subject_node(
            synthesis_text="Prior synthesis.",
            rebuilt_at=rebuild_time,
        )
        mock_service.get_entity_by_name.return_value = node

        obs_node = make_entity_node(uuid="obs-new", name="new observation", labels=["Observation"])
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1",
                source_node_uuid="subject-uuid",
                target_node_uuid="obs-new",
                fact="A new identity observation",
                created_at=rebuild_time + timedelta(hours=1),
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = obs_node

        result = await bootstrap(service=mock_service)

        assert len(result["delta"]) == 1
        assert result["delta"][0]["fact"] == "A new identity observation"


# ---------------------------------------------------------------------------
# No data scenarios
# ---------------------------------------------------------------------------

class TestBootstrapNoData:
    async def test_no_subject_node_returns_null_synthesis(self, mock_service):
        """When no subject Person node exists, returns null synthesis with message."""
        from pratyabhijna.tools.bootstrap import bootstrap

        mock_service.get_entity_by_name.return_value = None

        result = await bootstrap(service=mock_service)

        assert result["synthesis"] is None
        assert "message" in result

    async def test_subject_node_exists_but_no_synthesis(self, mock_service):
        """Subject node exists but has never had synthesis generated."""
        from pratyabhijna.tools.bootstrap import bootstrap

        node = make_subject_node(synthesis_text=None)
        mock_service.get_entity_by_name.return_value = node
        mock_service.get_edges_for_node.return_value = []

        result = await bootstrap(service=mock_service)

        assert result["synthesis"] is None
        assert result["rebuilt_at"] is None
        assert result["delta"] == []


# ---------------------------------------------------------------------------
# Contract: pure read, no side effects
# ---------------------------------------------------------------------------

class TestBootstrapContract:
    async def test_does_not_accept_queue_parameter(self):
        """bootstrap() takes only service — no queue parameter."""
        from pratyabhijna.tools.bootstrap import bootstrap

        sig = inspect.signature(bootstrap)
        param_names = set(sig.parameters.keys())
        assert "queue" not in param_names
        assert "service" in param_names

    async def test_does_not_trigger_rebuild(self, mock_service):
        """Even with very old synthesis, bootstrap just returns it. No side effects."""
        from pratyabhijna.tools.bootstrap import bootstrap

        old_time = datetime.now(timezone.utc) - timedelta(hours=100)
        node = make_subject_node(
            synthesis_text="Very old synthesis.",
            rebuilt_at=old_time,
        )
        mock_service.get_entity_by_name.return_value = node

        result = await bootstrap(service=mock_service)

        assert result["synthesis"] == "Very old synthesis."
        # No rebuild_triggered key — that concept doesn't exist here
        assert "rebuild_triggered" not in result
