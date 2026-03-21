"""The `history` MCP tool.

Finds an entity by name and returns a chronological timeline of all
edges — showing what was believed when, and what superseded what.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


async def history(
    service: PratyabhijnaService,
    entity_name: str,
) -> dict:
    """Get the chronological timeline of an entity's relationships.

    Args:
        service: The PratyabhijnaService instance.
        entity_name: Name of the entity to look up.

    Returns:
        Dict with entity info, count, and chronological timeline.
        Returns error dict if entity not found.
    """
    entity = await service.get_entity_by_name(entity_name)
    if entity is None:
        return {
            "error": "not_found",
            "message": f"No entity found matching '{entity_name}'.",
        }

    edges = await service.get_edges_for_node(entity.uuid)

    # Sort chronologically by valid_at, falling back to created_at
    def sort_key(edge):
        date = edge.valid_at or edge.created_at
        return date if date else ""

    edges.sort(key=sort_key)

    timeline = []
    for edge in edges:
        timeline.append({
            "uuid": edge.uuid,
            "name": edge.name,
            "fact": edge.fact,
            "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
            "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
            "created_at": edge.created_at.isoformat() if edge.created_at else None,
            "source_node_uuid": edge.source_node_uuid,
            "target_node_uuid": edge.target_node_uuid,
        })

    return {
        "entity": {
            "uuid": entity.uuid,
            "name": entity.name,
            "labels": entity.labels,
            "summary": entity.summary,
        },
        "count": len(timeline),
        "timeline": timeline,
    }
