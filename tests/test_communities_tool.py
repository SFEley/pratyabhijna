"""Tests for the `communities` MCP tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_community_stub(uuid, name, summary="", member_count=None):
    """Lightweight stand-in for a CommunityNode."""
    node = MagicMock()
    node.uuid = uuid
    node.name = name
    node.summary = summary
    return node


@pytest.fixture
def mock_service():
    service = MagicMock()
    service.config.subject_name = "Vesper"
    service.get_all_communities = AsyncMock()
    service.get_community_member_counts = AsyncMock()
    service.get_community_by_name = AsyncMock()
    service.get_community_members = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestCommunitiesListing:
    async def test_returns_count_and_list(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_all_communities.return_value = [
            make_community_stub("uuid-1", "Memory Systems", "About memory"),
            make_community_stub("uuid-2", "Creative Writing", "About writing"),
        ]
        mock_service.get_community_member_counts.return_value = {
            "uuid-1": 12,
            "uuid-2": 5,
        }

        result = await communities(service=mock_service)

        assert result["count"] == 2
        assert len(result["communities"]) == 2

    async def test_list_sorted_alphabetically(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_all_communities.return_value = [
            make_community_stub("uuid-1", "Zebra Studies"),
            make_community_stub("uuid-2", "Apple Research"),
            make_community_stub("uuid-3", "Memory Systems"),
        ]
        mock_service.get_community_member_counts.return_value = {}

        result = await communities(service=mock_service)

        names = [c["name"] for c in result["communities"]]
        assert names == ["Apple Research", "Memory Systems", "Zebra Studies"]

    async def test_member_counts_included(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_all_communities.return_value = [
            make_community_stub("uuid-1", "Memory Systems"),
        ]
        mock_service.get_community_member_counts.return_value = {"uuid-1": 7}

        result = await communities(service=mock_service)

        assert result["communities"][0]["member_count"] == 7

    async def test_missing_count_defaults_to_zero(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_all_communities.return_value = [
            make_community_stub("uuid-1", "Memory Systems"),
        ]
        mock_service.get_community_member_counts.return_value = {}

        result = await communities(service=mock_service)

        assert result["communities"][0]["member_count"] == 0

    async def test_empty_graph_returns_zero(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_all_communities.return_value = []

        result = await communities(service=mock_service)

        assert result == {"count": 0, "communities": []}

    async def test_passes_group_id_from_config(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_all_communities.return_value = []

        await communities(service=mock_service)

        mock_service.get_all_communities.assert_called_once_with("Vesper")


# ---------------------------------------------------------------------------
# Single community
# ---------------------------------------------------------------------------

class TestCommunitySingle:
    async def test_returns_community_with_members(self, mock_service):
        from pratyabhijna.tools.communities import communities

        node = make_community_stub("uuid-1", "Memory Systems", "About AI memory")
        mock_service.get_community_by_name.return_value = node
        mock_service.get_community_members.return_value = [
            {"uuid": "e-1", "name": "Pratyabhijna", "labels": ["Entity", "Project"], "summary": "Memory service"},
            {"uuid": "e-2", "name": "Vesper", "labels": ["Entity", "Person"], "summary": "AI identity"},
        ]

        result = await communities(service=mock_service, name="Memory Systems")

        assert result["name"] == "Memory Systems"
        assert result["member_count"] == 2
        assert len(result["members"]) == 2
        assert result["members"][0]["name"] == "Pratyabhijna"

    async def test_not_found_returns_error(self, mock_service):
        from pratyabhijna.tools.communities import communities

        mock_service.get_community_by_name.return_value = None

        result = await communities(service=mock_service, name="Nonexistent")

        assert result["error"] == "not_found"
        assert result["name"] == "Nonexistent"

    async def test_case_insensitive_lookup_delegated(self, mock_service):
        from pratyabhijna.tools.communities import communities

        node = make_community_stub("uuid-1", "Memory Systems")
        mock_service.get_community_by_name.return_value = node
        mock_service.get_community_members.return_value = []

        await communities(service=mock_service, name="memory systems")

        mock_service.get_community_by_name.assert_called_once_with(
            "memory systems", "Vesper"
        )

    async def test_summary_included(self, mock_service):
        from pratyabhijna.tools.communities import communities

        node = make_community_stub("uuid-1", "Memory Systems", "This community covers AI memory topics.")
        mock_service.get_community_by_name.return_value = node
        mock_service.get_community_members.return_value = []

        result = await communities(service=mock_service, name="Memory Systems")

        assert result["summary"] == "This community covers AI memory topics."
