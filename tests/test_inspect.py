"""Tests for the ``inspect`` MCP tool.

The inspect tool takes a UUID *or* a name and returns full detail.
UUIDs (canonical 8-4-4-4-12 hex) dispatch to typed UUID lookups across
Entity, Edge, Episodic, Community, and Saga. Names dispatch to exact
(case-insensitive) name lookups across Entity, Episodic, and Community
— no fuzzy fallback. Episodic-by-name returns the latest by created_at
and warns when multiple same-named nodes exist (a likely-dedupable
state).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError

from helpers import make_entity_edge, make_entity_node, make_episodic_node


# ---------------------------------------------------------------------------
# UUID-shaped strings for tests (the tool's regex rejects "node-1" etc.).
# ---------------------------------------------------------------------------

UUID_NODE_1 = "11111111-1111-4111-8111-111111111111"
UUID_NODE_2 = "22222222-2222-4222-8222-222222222222"
UUID_NODE_3 = "33333333-3333-4333-8333-333333333333"
UUID_EDGE = "edededed-eded-4eed-8eed-edededededed"
UUID_EPISODE = "ee5e5e5e-5e5e-4e5e-8e5e-5e5e5e5e5e5e"
UUID_COMMUNITY = "c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0"
UUID_SAGA = "5a6a5a6a-5a6a-4a6a-8a6a-5a6a5a6a5a6a"
UUID_UNKNOWN = "deadbeef-dead-4eef-8eef-deadbeefdead"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock service whose UUID/name lookups default to NotFound.

    Individual tests override the path they exercise. Keeping the
    defaults as misses ensures a test for Entity lookup can't
    accidentally pass via an unconfigured Episodic-or-later fallback.
    """
    service = MagicMock()
    service.is_connected = True
    service.config.subject_name = "vesper"
    service.get_entity_by_uuid = AsyncMock(
        side_effect=NodeNotFoundError(UUID_UNKNOWN),
    )
    service.get_edge = AsyncMock(side_effect=EdgeNotFoundError(UUID_UNKNOWN))
    service.get_episode_by_uuid = AsyncMock(
        side_effect=NodeNotFoundError(UUID_UNKNOWN),
    )
    service.get_community_by_uuid = AsyncMock(
        side_effect=NodeNotFoundError(UUID_UNKNOWN),
    )
    service.get_saga_by_uuid = AsyncMock(
        side_effect=NodeNotFoundError(UUID_UNKNOWN),
    )
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_episodes_for_node = AsyncMock(return_value=[])
    service.get_episodes_by_uuids = AsyncMock(return_value=[])
    service.get_entity_by_name = AsyncMock(return_value=None)
    service.get_latest_episode_by_name = AsyncMock(return_value=None)
    service.get_community_by_name = AsyncMock(return_value=None)
    return service


# ---------------------------------------------------------------------------
# UUID lookup: Entity
# ---------------------------------------------------------------------------

class TestInspectEntity:
    async def test_inspect_entity(self, mock_service):
        """UUID resolving to a node returns full entity detail."""
        from pratyabhijna.tools.inspect import inspect

        entity = make_entity_node(
            uuid=UUID_NODE_1,
            name="Serah",
            labels=["Person"],
            summary="Host alter, software engineer",
            attributes={"gender": "female", "person_type": "alter"},
        )
        edges = [
            make_entity_edge(
                uuid=UUID_EDGE,
                fact="Serah values directness",
                source_node_uuid=UUID_NODE_1,
                target_node_uuid=UUID_NODE_2,
            ),
        ]
        episodes = [
            make_episodic_node(uuid=UUID_EPISODE, content="Serah said she values directness."),
        ]
        mock_service.get_entity_by_uuid = AsyncMock(return_value=entity)
        mock_service.get_edges_for_node = AsyncMock(return_value=edges)
        mock_service.get_episodes_for_node = AsyncMock(return_value=episodes)

        result = await inspect(service=mock_service, identifier=UUID_NODE_1)

        assert result["type"] == "entity"
        assert result["uuid"] == UUID_NODE_1
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

        entity = make_entity_node(uuid=UUID_NODE_1, name="Serah")
        edges = [
            make_entity_edge(
                uuid="edededed-eded-4eed-8eed-edededededed",
                fact="Serah values directness",
                source_node_uuid=UUID_NODE_1,
                target_node_uuid=UUID_NODE_2,
            ),
            make_entity_edge(
                uuid="cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd",
                fact="Vesper was created by Serah",
                source_node_uuid=UUID_NODE_3,
                target_node_uuid=UUID_NODE_1,
            ),
        ]
        mock_service.get_entity_by_uuid = AsyncMock(return_value=entity)
        mock_service.get_edges_for_node = AsyncMock(return_value=edges)

        result = await inspect(service=mock_service, identifier=UUID_NODE_1)

        edge_out = [e for e in result["edges"] if e["source_node_uuid"] == UUID_NODE_1][0]
        edge_in = [e for e in result["edges"] if e["target_node_uuid"] == UUID_NODE_1][0]
        assert edge_out["direction"] == "outgoing"
        assert edge_in["direction"] == "incoming"


# ---------------------------------------------------------------------------
# UUID lookup: Edge
# ---------------------------------------------------------------------------

class TestInspectEdge:
    async def test_inspect_edge(self, mock_service):
        """A UUID matching no Entity but an Edge returns edge detail."""
        from pratyabhijna.tools.inspect import inspect

        edge = make_entity_edge(
            uuid=UUID_EDGE,
            name="values",
            fact="Serah values directness",
            source_node_uuid=UUID_NODE_1,
            target_node_uuid=UUID_NODE_2,
            valid_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
            episodes=[UUID_EPISODE],
        )
        mock_service.get_edge = AsyncMock(return_value=edge)

        # Entity-by-UUID is called twice: once for the initial dispatch
        # (raises NotFound — falls through to edge lookup), then twice
        # more inside _format_edge to resolve source/target endpoints.
        source_node = make_entity_node(uuid=UUID_NODE_1, name="Serah", labels=["Person"])
        target_node = make_entity_node(uuid=UUID_NODE_2, name="directness", labels=["Observation"])
        call_count = 0

        async def get_entity_dispatch(uuid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise NodeNotFoundError(uuid)
            return {UUID_NODE_1: source_node, UUID_NODE_2: target_node}[uuid]

        mock_service.get_entity_by_uuid = AsyncMock(side_effect=get_entity_dispatch)
        mock_service.get_episodes_by_uuids = AsyncMock(
            return_value=[make_episodic_node(uuid=UUID_EPISODE)],
        )

        result = await inspect(service=mock_service, identifier=UUID_EDGE)

        assert result["type"] == "edge"
        assert result["uuid"] == UUID_EDGE
        assert result["name"] == "values"
        assert result["fact"] == "Serah values directness"
        assert result["source_entity"]["name"] == "Serah"
        assert result["target_entity"]["name"] == "directness"
        assert result["valid_at"] is not None
        assert result["invalid_at"] is None


# ---------------------------------------------------------------------------
# UUID lookup: Episodic (the bug-fix case)
# ---------------------------------------------------------------------------

class TestInspectEpisodic:
    """An Episodic UUID used to return ``not_found`` because the prior
    inspect only tried Entity and Edge. With the third fallback wired,
    an Episodic UUID resolves to a typed Episodic detail dict."""

    async def test_inspect_episodic_by_uuid(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        episode = make_episodic_node(
            uuid=UUID_EPISODE,
            content="Today we shipped PR3.",
            source_description="vesper",
        )
        mock_service.get_episode_by_uuid = AsyncMock(return_value=episode)

        result = await inspect(service=mock_service, identifier=UUID_EPISODE)

        assert result["type"] == "episodic"
        assert result["uuid"] == UUID_EPISODE
        # The helper sets name to f"episode:{uuid}"; verify that flows through.
        assert result["name"].startswith("episode:")
        assert result["content"] == "Today we shipped PR3."
        assert result["source_description"] == "vesper"
        assert result["created_at"] is not None

    async def test_episodic_uuid_tried_after_entity_and_edge(self, mock_service):
        """Order matters: only after Entity and Edge both miss does the
        Episodic fallback run. Confirms the fallback isn't short-circuit."""
        from pratyabhijna.tools.inspect import inspect

        episode = make_episodic_node(uuid=UUID_EPISODE)
        mock_service.get_episode_by_uuid = AsyncMock(return_value=episode)

        await inspect(service=mock_service, identifier=UUID_EPISODE)

        mock_service.get_entity_by_uuid.assert_awaited_once_with(UUID_EPISODE)
        mock_service.get_edge.assert_awaited_once_with(UUID_EPISODE)
        mock_service.get_episode_by_uuid.assert_awaited_once_with(UUID_EPISODE)


# ---------------------------------------------------------------------------
# UUID lookup: Community / Saga (lower-priority fallbacks)
# ---------------------------------------------------------------------------

class TestInspectCommunityAndSaga:
    async def test_inspect_community_by_uuid(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        community = SimpleNamespace(
            uuid=UUID_COMMUNITY, name="Phase-7 work", group_id="default",
            summary="Auth, deployment, observability.",
            created_at=datetime(2026, 4, 11, tzinfo=timezone.utc),
        )
        mock_service.get_community_by_uuid = AsyncMock(return_value=community)

        result = await inspect(service=mock_service, identifier=UUID_COMMUNITY)

        assert result["type"] == "community"
        assert result["uuid"] == UUID_COMMUNITY
        assert result["name"] == "Phase-7 work"

    async def test_inspect_saga_by_uuid(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        saga = SimpleNamespace(
            uuid=UUID_SAGA, name="solo-sessions", group_id="default",
            created_at=datetime(2026, 3, 18, tzinfo=timezone.utc),
        )
        mock_service.get_saga_by_uuid = AsyncMock(return_value=saga)

        result = await inspect(service=mock_service, identifier=UUID_SAGA)

        assert result["type"] == "saga"
        assert result["uuid"] == UUID_SAGA
        assert result["name"] == "solo-sessions"


# ---------------------------------------------------------------------------
# Name lookup
# ---------------------------------------------------------------------------

class TestInspectByName:
    """A non-UUID string is treated as a name and resolved across
    Entity → Episodic → Community (exact match, case-insensitive)."""

    async def test_name_dispatches_to_entity(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        entity = make_entity_node(
            uuid=UUID_NODE_1, name="Serah", labels=["Person"],
        )
        mock_service.get_entity_by_name = AsyncMock(return_value=entity)

        result = await inspect(service=mock_service, identifier="Serah")

        assert result["type"] == "entity"
        assert result["name"] == "Serah"
        # UUID-shaped dispatch must not have been attempted on a name.
        mock_service.get_entity_by_uuid.assert_not_awaited()

    async def test_name_lookup_rejects_fuzzy_entity_match(self, mock_service):
        """``get_entity_by_name`` falls back to the closest semantic match
        if no exact name hit — but for `inspect`, we want deterministic
        lookups, so we reject anything that isn't the exact (case-
        insensitive) name and try the next fallback instead."""
        from pratyabhijna.tools.inspect import inspect

        # Service returns a fuzzy match — different name from the query.
        fuzzy_entity = make_entity_node(
            uuid=UUID_NODE_1, name="Serah Eley", labels=["Person"],
        )
        mock_service.get_entity_by_name = AsyncMock(return_value=fuzzy_entity)
        # Episodic-by-name also misses — should fall through to not_found.
        mock_service.get_latest_episode_by_name = AsyncMock(return_value=None)

        result = await inspect(service=mock_service, identifier="Serah")

        # The fuzzy hit was rejected; final result is not_found.
        assert result["error"] == "not_found"
        assert result["lookup"] == "name"

    async def test_name_dispatches_to_episodic_when_entity_misses(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        episode = make_episodic_node(
            uuid=UUID_EPISODE,
            content="On the porch, looking at the river.",
        )
        # Force the Episodic name to match the lookup string.
        episode.name = "writing/solo-23-the-woman-on-the-porch.md"
        mock_service.get_entity_by_name = AsyncMock(return_value=None)
        mock_service.get_latest_episode_by_name = AsyncMock(return_value=episode)
        # Single-copy case: no disambiguation warning.
        driver = MagicMock()
        driver.execute_query = AsyncMock(return_value=([{"c": 1}], None, None))
        mock_service._graphiti = MagicMock()
        mock_service._graphiti.driver = driver

        result = await inspect(
            service=mock_service,
            identifier="writing/solo-23-the-woman-on-the-porch.md",
        )

        assert result["type"] == "episodic"
        assert result["uuid"] == UUID_EPISODE
        assert "warning" not in result

    async def test_episodic_name_collision_surfaces_warning(self, mock_service):
        """When multiple Episodics share a name, the latest wins but the
        response calls out the ambiguity so the caller can dedupe."""
        from pratyabhijna.tools.inspect import inspect

        episode = make_episodic_node(uuid=UUID_EPISODE)
        episode.name = "TO_SERAH.md"
        mock_service.get_latest_episode_by_name = AsyncMock(return_value=episode)
        driver = MagicMock()
        driver.execute_query = AsyncMock(return_value=([{"c": 3}], None, None))
        mock_service._graphiti = MagicMock()
        mock_service._graphiti.driver = driver

        result = await inspect(service=mock_service, identifier="TO_SERAH.md")

        assert result["type"] == "episodic"
        assert "warning" in result
        assert "3" in result["warning"]
        assert "TO_SERAH.md" in result["warning"]

    async def test_name_dispatches_to_community_when_others_miss(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        community = SimpleNamespace(
            uuid=UUID_COMMUNITY, name="Phase-7 work", group_id="default",
            summary="Auth, deployment, observability.",
            created_at=datetime(2026, 4, 11, tzinfo=timezone.utc),
        )
        mock_service.get_community_by_name = AsyncMock(return_value=community)

        result = await inspect(service=mock_service, identifier="Phase-7 work")

        assert result["type"] == "community"
        assert result["uuid"] == UUID_COMMUNITY
        mock_service.get_community_by_name.assert_awaited_once_with(
            "Phase-7 work", "vesper"
        )

    async def test_name_not_found(self, mock_service):
        from pratyabhijna.tools.inspect import inspect

        result = await inspect(service=mock_service, identifier="Nobody Here")

        assert result["error"] == "not_found"
        assert result["identifier"] == "Nobody Here"
        assert result["lookup"] == "name"
        assert "exact" in result["message"]


# ---------------------------------------------------------------------------
# Not found (UUID path)
# ---------------------------------------------------------------------------

class TestInspectNotFound:
    async def test_uuid_not_found_in_any_typed_lookup(self, mock_service):
        """A UUID that misses Entity, Edge, Episodic, Community, and
        Saga returns ``not_found`` with the lookup mode named."""
        from pratyabhijna.tools.inspect import inspect

        result = await inspect(service=mock_service, identifier=UUID_UNKNOWN)

        assert result["error"] == "not_found"
        assert result["identifier"] == UUID_UNKNOWN
        assert result["lookup"] == "uuid"
        assert "message" in result
