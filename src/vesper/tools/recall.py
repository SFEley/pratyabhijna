"""The `recall` MCP tool.

Synchronous hybrid search (semantic + keyword + graph traversal)
via the service layer. Returns ranked results with entity names
resolved for edges.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from graphiti_core.search.search_filters import (
    ComparisonOperator,
    DateFilter,
    SearchFilters,
)

if TYPE_CHECKING:
    from vesper.service import VesperService


def _parse_time_range(time_range: str) -> list[list[DateFilter]]:
    """Parse a time range string into DateFilter groups.

    Supports:
    - Relative: "7d", "24h", "30d"
    - Absolute: "2025-01-01..2025-03-01"

    Returns a list containing one OR-group of DateFilters.
    """
    # Relative: "7d", "24h"
    relative = re.match(r"^(\d+)([dhm])$", time_range.strip())
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "m": timedelta(minutes=amount),
        }[unit]
        cutoff = datetime.now(timezone.utc) - delta
        return [[
            DateFilter(
                date=cutoff,
                comparison_operator=ComparisonOperator.greater_than_equal,
            ),
        ]]

    # Absolute: "2025-01-01..2025-03-01"
    if ".." in time_range:
        start_str, end_str = time_range.split("..", 1)
        start = datetime.fromisoformat(start_str.strip()).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_str.strip()).replace(tzinfo=timezone.utc)
        return [[
            DateFilter(
                date=start,
                comparison_operator=ComparisonOperator.greater_than_equal,
            ),
            DateFilter(
                date=end,
                comparison_operator=ComparisonOperator.less_than_equal,
            ),
        ]]

    raise ValueError(f"Unrecognized time_range format: {time_range!r}")


async def recall(
    service: VesperService,
    query: str,
    memory_type: str | None = None,
    time_range: str | None = None,
) -> dict:
    """Search the knowledge graph and return ranked results.

    Args:
        service: The VesperService instance.
        query: Natural language search query.
        memory_type: Optional entity type filter (e.g. "Person", "Observation").
        time_range: Optional time filter — relative ("7d") or absolute
            ("2025-01-01..2025-03-01").

    Returns:
        Dict with query echo, count, and results list sorted by score.
    """
    # Build search filter from optional parameters
    search_filter = None
    node_labels = [memory_type] if memory_type else None
    try:
        created_at = _parse_time_range(time_range) if time_range else None
    except ValueError as e:
        return {"error": "invalid_time_range", "message": str(e)}

    if node_labels or created_at:
        search_filter = SearchFilters(
            node_labels=node_labels,
            created_at=created_at,
        )

    results = await service.recall(query=query, search_filter=search_filter)

    # Build a node lookup for resolving edge endpoints
    node_map = {node.uuid: node for node in (results.nodes or [])}

    # Merge edges and nodes into a single ranked list
    formatted = []

    for i, edge in enumerate(results.edges or []):
        score = (results.edge_reranker_scores or [])[i] if i < len(results.edge_reranker_scores or []) else 0.0
        source = node_map.get(edge.source_node_uuid)
        target = node_map.get(edge.target_node_uuid)
        formatted.append({
            "type": "edge",
            "uuid": edge.uuid,
            "name": edge.name,
            "fact": edge.fact,
            "score": score,
            "source_entity": source.name if source else None,
            "target_entity": target.name if target else None,
            "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
            "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
        })

    for i, node in enumerate(results.nodes or []):
        score = (results.node_reranker_scores or [])[i] if i < len(results.node_reranker_scores or []) else 0.0
        formatted.append({
            "type": "node",
            "uuid": node.uuid,
            "name": node.name,
            "labels": node.labels,
            "summary": node.summary,
            "score": score,
        })

    # Sort by score descending
    formatted.sort(key=lambda r: r["score"], reverse=True)

    return {
        "query": query,
        "count": len(formatted),
        "results": formatted,
    }
