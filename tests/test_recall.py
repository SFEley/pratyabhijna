"""Tests for the `recall` MCP tool.

TDD: these tests define the recall tool contract. They should fail
until recall.py is implemented and wired into the server.

The recall tool performs synchronous hybrid search (semantic + keyword +
graph traversal) via the service layer and returns ranked results.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_filters import (
    ComparisonOperator,
    SearchFilters,
)

from helpers import make_entity_edge, make_entity_node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService with recall method."""
    service = MagicMock()
    service.is_connected = True
    service.recall = AsyncMock()
    return service


# ---------------------------------------------------------------------------
# Basic search
# ---------------------------------------------------------------------------

class TestRecallResults:
    async def test_recall_returns_results(self, mock_service):
        """recall() returns a dict with results list, count, and query echo."""
        from pratyabhijna.tools.recall import recall

        node1 = make_entity_node(uuid="node-1", name="Serah")
        node2 = make_entity_node(uuid="node-2", name="directness", labels=["Position"])
        edge1 = make_entity_edge(
            uuid="edge-1",
            fact="Serah values directness",
            source_node_uuid="node-1",
            target_node_uuid="node-2",
        )
        mock_service.recall.return_value = SearchResults(
            edges=[edge1],
            edge_reranker_scores=[0.87],
            nodes=[node1, node2],
            node_reranker_scores=[0.75, 0.60],
        )

        result = await recall(service=mock_service, query="Serah's preferences")

        assert result["query"] == "Serah's preferences"
        assert result["count"] >= 1
        assert isinstance(result["results"], list)
        assert len(result["results"]) >= 1
        # Each result should have essential fields
        first = result["results"][0]
        assert "uuid" in first
        assert "score" in first
        assert first["type"] in ("edge", "node")

    async def test_recall_empty_results(self, mock_service):
        """Empty search returns results: [], count: 0 — not an error."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = SearchResults()

        result = await recall(service=mock_service, query="nonexistent topic")

        assert result["results"] == []
        assert result["count"] == 0
        assert "error" not in result


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestRecallFilters:
    async def test_recall_with_type_filter(self, mock_service):
        """memory_type='Person' passes node_labels=['Person'] to service."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = SearchResults()

        await recall(service=mock_service, query="people", memory_type="Person")

        call_kwargs = mock_service.recall.call_args
        search_filter = call_kwargs.kwargs.get("search_filter") or call_kwargs[1].get("search_filter")
        assert search_filter is not None
        assert search_filter.node_labels == ["Person"]

    async def test_recall_with_time_range_relative(self, mock_service):
        """time_range='7d' builds a created_at filter >= 7 days ago."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = SearchResults()
        before = datetime.now(timezone.utc) - timedelta(days=7, seconds=5)

        await recall(service=mock_service, query="recent", time_range="7d")

        call_kwargs = mock_service.recall.call_args
        search_filter = call_kwargs.kwargs.get("search_filter") or call_kwargs[1].get("search_filter")
        assert search_filter is not None
        assert search_filter.created_at is not None
        # Should have a >= filter for approximately 7 days ago
        date_filters = search_filter.created_at[0]  # first OR group
        gte_filter = [f for f in date_filters if f.comparison_operator == ComparisonOperator.greater_than_equal]
        assert len(gte_filter) == 1
        assert gte_filter[0].date >= before

    async def test_recall_with_time_range_absolute(self, mock_service):
        """time_range='2025-01-01..2025-03-01' builds a date range filter."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = SearchResults()

        await recall(
            service=mock_service,
            query="early 2025",
            time_range="2025-01-01..2025-03-01",
        )

        call_kwargs = mock_service.recall.call_args
        search_filter = call_kwargs.kwargs.get("search_filter") or call_kwargs[1].get("search_filter")
        assert search_filter is not None
        assert search_filter.created_at is not None
        date_filters = search_filter.created_at[0]
        # Should have both >= start and <= end
        operators = {f.comparison_operator for f in date_filters}
        assert ComparisonOperator.greater_than_equal in operators
        assert ComparisonOperator.less_than_equal in operators

    async def test_recall_invalid_time_range_returns_error(self, mock_service):
        """Invalid time_range returns error dict, not an exception."""
        from pratyabhijna.tools.recall import recall

        result = await recall(
            service=mock_service,
            query="test",
            time_range="not-a-valid-range",
        )

        assert result["error"] == "invalid_time_range"
        assert "message" in result
        # Service should not have been called
        mock_service.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

class TestRecallFormatting:
    async def test_recall_edge_results_include_entity_names(self, mock_service):
        """Edge results resolve source/target node UUIDs to entity names."""
        from pratyabhijna.tools.recall import recall

        node1 = make_entity_node(uuid="node-1", name="Serah")
        node2 = make_entity_node(uuid="node-2", name="directness", labels=["Position"])
        edge1 = make_entity_edge(
            source_node_uuid="node-1",
            target_node_uuid="node-2",
            fact="Serah values directness",
        )
        mock_service.recall.return_value = SearchResults(
            edges=[edge1],
            edge_reranker_scores=[0.9],
            nodes=[node1, node2],
            node_reranker_scores=[0.8, 0.7],
        )

        result = await recall(service=mock_service, query="values")

        edge_results = [r for r in result["results"] if r["type"] == "edge"]
        assert len(edge_results) >= 1
        assert edge_results[0]["source_entity"] == "Serah"
        assert edge_results[0]["target_entity"] == "directness"

    async def test_recall_edge_missing_node_gets_null_name(self, mock_service):
        """Edge referencing a node not in results gets name: None."""
        from pratyabhijna.tools.recall import recall

        # Edge references node-99 which is NOT in the nodes list
        edge1 = make_entity_edge(
            source_node_uuid="node-1",
            target_node_uuid="node-99",
            fact="Serah knows someone",
        )
        node1 = make_entity_node(uuid="node-1", name="Serah")
        mock_service.recall.return_value = SearchResults(
            edges=[edge1],
            edge_reranker_scores=[0.85],
            nodes=[node1],
            node_reranker_scores=[0.7],
        )

        result = await recall(service=mock_service, query="relationships")

        edge_results = [r for r in result["results"] if r["type"] == "edge"]
        assert len(edge_results) >= 1
        assert edge_results[0]["source_entity"] == "Serah"
        assert edge_results[0]["target_entity"] is None

    async def test_recall_results_sorted_by_score(self, mock_service):
        """Results are sorted by reranker score, highest first."""
        from pratyabhijna.tools.recall import recall

        edge_low = make_entity_edge(uuid="edge-low", fact="low score edge")
        edge_high = make_entity_edge(uuid="edge-high", fact="high score edge")
        node1 = make_entity_node(uuid="node-1", name="topic", labels=["Position"])
        mock_service.recall.return_value = SearchResults(
            edges=[edge_low, edge_high],
            edge_reranker_scores=[0.3, 0.9],
            nodes=[node1],
            node_reranker_scores=[0.5],
        )

        result = await recall(service=mock_service, query="test")

        scores = [r["score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


def _bulk_search_results(n_edges: int, n_nodes: int) -> SearchResults:
    """Build SearchResults with descending scores so sort order is deterministic."""
    edges = [
        make_entity_edge(uuid=f"edge-{i}", fact=f"edge fact {i}")
        for i in range(n_edges)
    ]
    edge_scores = [1.0 - (i * 0.01) for i in range(n_edges)]
    nodes = [
        make_entity_node(uuid=f"node-{i}", name=f"node {i}", labels=["Observation"])
        for i in range(n_nodes)
    ]
    # Node scores deliberately interleave with edge scores
    node_scores = [0.95 - (i * 0.01) for i in range(n_nodes)]
    return SearchResults(
        edges=edges,
        edge_reranker_scores=edge_scores,
        nodes=nodes,
        node_reranker_scores=node_scores,
    )


class TestRecallLimit:
    async def test_recall_default_limit_is_5(self, mock_service):
        """Default limit caps results at 5."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = _bulk_search_results(n_edges=10, n_nodes=10)

        result = await recall(service=mock_service, query="anything")

        assert result["count"] == 5
        assert len(result["results"]) == 5

    async def test_recall_default_includes_total_before_limit(self, mock_service):
        """When the limit trims results, total_before_limit reports the pool size."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = _bulk_search_results(n_edges=10, n_nodes=10)

        result = await recall(service=mock_service, query="anything")

        assert result["total_before_limit"] == 20

    async def test_recall_custom_limit_widens_pool(self, mock_service):
        """A larger limit returns more results when the pool supports it."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = _bulk_search_results(n_edges=10, n_nodes=10)

        result = await recall(service=mock_service, query="anything", limit=15)

        assert result["count"] == 15
        assert result["total_before_limit"] == 20

    async def test_recall_limit_does_not_exceed_pool(self, mock_service):
        """When the pool is smaller than the limit, no trimming occurs and no
        total_before_limit field is added."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = _bulk_search_results(n_edges=2, n_nodes=2)

        result = await recall(service=mock_service, query="anything", limit=10)

        assert result["count"] == 4
        assert "total_before_limit" not in result

    async def test_recall_limit_keeps_highest_scoring_results(self, mock_service):
        """Sliced results are the top N by score, not an arbitrary slice."""
        from pratyabhijna.tools.recall import recall

        mock_service.recall.return_value = _bulk_search_results(n_edges=10, n_nodes=10)

        result = await recall(service=mock_service, query="anything", limit=3)

        scores = [r["score"] for r in result["results"]]
        # Top 3 from a pool of 20 with scores in [0.81..1.00] should be near 1.00
        assert scores == sorted(scores, reverse=True)
        assert all(s >= 0.95 for s in scores)
