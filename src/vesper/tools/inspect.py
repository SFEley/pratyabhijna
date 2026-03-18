"""The `inspect` MCP tool.

Takes a UUID and returns full detail — either an entity node
(with connected edges and episodes) or an edge (with resolved
source/target entities and episode provenance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError

if TYPE_CHECKING:
    from vesper.service import VesperService


async def inspect(
    service: VesperService,
    uuid: str,
) -> dict:
    """Get full detail for a node or edge by UUID.

    Tries node lookup first, falls back to edge lookup.

    Args:
        service: The VesperService instance.
        uuid: UUID of the entity node or edge.

    Returns:
        Full detail dict (type varies by node vs edge),
        or error dict if not found.
    """
    # Try as entity node first
    try:
        entity = await service.get_entity(uuid)
        return await _format_entity(service, entity, uuid)
    except NodeNotFoundError:
        pass

    # Try as edge
    try:
        edge = await service.get_edge(uuid)
        return await _format_edge(service, edge)
    except EdgeNotFoundError:
        pass

    return {
        "error": "not_found",
        "uuid": uuid,
        "message": f"No entity or edge found with UUID '{uuid}'.",
    }


async def _format_entity(service, entity, uuid: str) -> dict:
    """Format a full entity detail response."""
    edges = await service.get_edges_for_node(uuid)
    episodes = await service.get_episodes_for_node(uuid)

    formatted_edges = []
    for edge in edges:
        direction = "outgoing" if edge.source_node_uuid == uuid else "incoming"
        formatted_edges.append({
            "uuid": edge.uuid,
            "name": edge.name,
            "fact": edge.fact,
            "direction": direction,
            "source_node_uuid": edge.source_node_uuid,
            "target_node_uuid": edge.target_node_uuid,
            "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
            "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
        })

    formatted_episodes = []
    for ep in episodes:
        formatted_episodes.append({
            "uuid": ep.uuid,
            "content": ep.content,
            "source_description": ep.source_description,
            "created_at": ep.created_at.isoformat() if ep.created_at else None,
        })

    return {
        "type": "entity",
        "uuid": entity.uuid,
        "name": entity.name,
        "labels": entity.labels,
        "summary": entity.summary,
        "attributes": entity.attributes,
        "created_at": entity.created_at.isoformat() if entity.created_at else None,
        "edges": formatted_edges,
        "episodes": formatted_episodes,
    }


async def _format_edge(service, edge) -> dict:
    """Format a full edge detail response."""
    # Resolve source and target entities
    source_entity = await _safe_get_entity(service, edge.source_node_uuid)
    target_entity = await _safe_get_entity(service, edge.target_node_uuid)

    # Fetch episode provenance
    episodes = await service.get_episodes_by_uuids(edge.episodes or [])
    formatted_episodes = []
    for ep in episodes:
        formatted_episodes.append({
            "uuid": ep.uuid,
            "content": ep.content,
            "source_description": ep.source_description,
            "created_at": ep.created_at.isoformat() if ep.created_at else None,
        })

    return {
        "type": "edge",
        "uuid": edge.uuid,
        "name": edge.name,
        "fact": edge.fact,
        "source_entity": _entity_summary(source_entity),
        "target_entity": _entity_summary(target_entity),
        "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
        "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
        "created_at": edge.created_at.isoformat() if edge.created_at else None,
        "attributes": edge.attributes,
        "episodes": formatted_episodes,
    }


async def _safe_get_entity(service, uuid: str):
    """Get entity by UUID, returning None instead of raising."""
    try:
        return await service.get_entity_by_uuid(uuid)
    except NodeNotFoundError:
        return None


def _entity_summary(entity) -> dict | None:
    """Compact entity representation for edge endpoints."""
    if entity is None:
        return None
    return {
        "uuid": entity.uuid,
        "name": entity.name,
        "labels": entity.labels,
    }
