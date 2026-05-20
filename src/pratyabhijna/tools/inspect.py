"""The ``inspect`` MCP tool.

Takes a UUID or a name and returns full detail — entity node (with
connected edges and episodes), edge (with resolved source/target and
provenance), episodic node (content + metadata + saga membership),
community node, or saga node. The input shape (UUID vs name) is
auto-detected: anything matching the canonical 8-4-4-4-12 hex pattern
is treated as a UUID and dispatched to the typed UUID lookups; any
other string is treated as a name and resolved against Entity then
Episodic (then Community by name as a third try).

Name lookups use exact (case-insensitive) match. Episodic names can
collide (multiple ingestions of the same file produce duplicate-named
episodes), so the Episodic-by-name path returns the *latest by
created_at* — call out the disambiguation explicitly in the response
so the caller knows it isn't a unique match.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError

if TYPE_CHECKING:
    from pratyabhijna.service import PratyabhijnaService


# Canonical 8-4-4-4-12 hex UUID. Case-insensitive. Anything else is
# treated as a name — names can legitimately contain hyphens and
# alphanumerics, so a stricter shape check than this is needed.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


async def inspect(
    service: PratyabhijnaService,
    identifier: str,
) -> dict:
    """Get full detail for a node or edge, by UUID or by name.

    Args:
        service: The PratyabhijnaService instance.
        identifier: A canonical UUID (8-4-4-4-12 hex) or a name. UUIDs
            dispatch to typed UUID lookups (Entity → Edge → Episodic →
            Community → Saga). Names dispatch to name lookups (Entity
            then Episodic; Community by name as a third try).

    Returns:
        Full detail dict (type varies by node vs edge), or error dict
        if no match found. For Episodic name lookups that resolved to
        the latest of several same-named episodes, the response
        includes a ``warning`` field naming the ambiguity.
    """
    if _UUID_RE.match(identifier):
        return await _inspect_by_uuid(service, identifier)
    return await _inspect_by_name(service, identifier)


async def _inspect_by_uuid(service, uuid: str) -> dict:
    """Try the typed UUID lookups in order: Entity, Edge, Episodic,
    Community, Saga. First hit wins; ``not_found`` if all five miss."""
    # Entity node
    try:
        entity = await service.get_entity_by_uuid(uuid)
        return await _format_entity(service, entity, uuid)
    except NodeNotFoundError:
        pass

    # Entity edge
    try:
        edge = await service.get_edge(uuid)
        return await _format_edge(service, edge)
    except EdgeNotFoundError:
        pass

    # Episodic node (the case the prior shape silently mishandled — an
    # Episodic UUID returned not_found even when the node existed, since
    # neither Entity nor Edge would match it.)
    try:
        episode = await service.get_episode_by_uuid(uuid)
        return _format_episodic(episode)
    except NodeNotFoundError:
        pass

    # Community node
    try:
        community = await service.get_community_by_uuid(uuid)
        return _format_community(community)
    except NodeNotFoundError:
        pass

    # Saga node
    try:
        saga = await service.get_saga_by_uuid(uuid)
        return _format_saga(saga)
    except NodeNotFoundError:
        pass

    return {
        "error": "not_found",
        "identifier": uuid,
        "lookup": "uuid",
        "message": (
            f"No entity, edge, episode, community, or saga found with "
            f"UUID '{uuid}'."
        ),
    }


async def _inspect_by_name(service, name: str) -> dict:
    """Resolve a name to an Entity, then Episodic, then Community.

    Entity name lookup uses ``get_entity_by_name``, which falls back to
    a fuzzy semantic match if no exact name hit — we accept only the
    case-insensitive exact match here so name lookup is deterministic.
    Episodic name lookup uses ``get_latest_episode_by_name`` and notes
    the ambiguity in the response when picking among same-named copies.
    """
    # Entity — strict exact-name only (the service helper does a fuzzy
    # fallback; reject that for inspect, where deterministic shape
    # matters more than best-effort matching).
    entity = await service.get_entity_by_name(name)
    if entity is not None and entity.name.lower() == name.lower():
        return await _format_entity(service, entity, entity.uuid)

    # Episodic — exact name, latest by created_at.
    episode = await service.get_latest_episode_by_name(name)
    if episode is not None:
        same_name_count = await _count_episodes_by_name(service, name)
        formatted = _format_episodic(episode)
        if same_name_count > 1:
            formatted["warning"] = (
                f"{same_name_count} Episodic nodes share the name "
                f"'{name}'; this is the latest by created_at. The others "
                f"are likely duplicate ingestions — consider a "
                f"forget-episode pass to dedupe."
            )
        return formatted

    # Community — exact name. ``get_community_by_name`` returns None on
    # miss rather than raising, so check directly.
    community = await service.get_community_by_name(name)
    if community is not None:
        return _format_community(community)

    return {
        "error": "not_found",
        "identifier": name,
        "lookup": "name",
        "message": (
            f"No entity, episode, or community found with the exact "
            f"name '{name}'. Name lookup is case-insensitive but exact "
            f"(no fuzzy fallback). For semantic search, use `recall`."
        ),
    }


async def _count_episodes_by_name(service, name: str) -> int:
    """How many Episodic nodes share this name. Cheap Cypher count for
    the disambiguation warning on Episodic-by-name lookup."""
    driver = service._graphiti.driver
    records, _, _ = await driver.execute_query(
        "MATCH (e:Episodic {name: $name}) RETURN count(e) AS c",
        name=name,
        routing_="r",
    )
    return int(records[0]["c"]) if records else 0


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


def _format_episodic(episode) -> dict:
    """Format an episodic node — content, dates, source, provenance.

    Deliberately does not enumerate the entities this episode mentions
    (that's a separate Cypher fan-out and the count is already implied
    by the entity edges' episode-provenance fields when inspecting from
    the entity side). The point of inspecting an episode is "is it
    there, what does it say, where did it come from."
    """
    return {
        "type": "episodic",
        "uuid": episode.uuid,
        "name": episode.name,
        "group_id": episode.group_id,
        "source": getattr(episode.source, "value", str(episode.source)),
        "source_description": episode.source_description,
        "content": episode.content,
        "created_at": episode.created_at.isoformat() if episode.created_at else None,
        "valid_at": episode.valid_at.isoformat() if episode.valid_at else None,
    }


def _format_community(community) -> dict:
    """Format a community node — summary + member count. Members
    themselves are best fetched via the `communities` tool when needed,
    not inlined here."""
    return {
        "type": "community",
        "uuid": community.uuid,
        "name": community.name,
        "summary": getattr(community, "summary", None),
        "group_id": community.group_id,
        "created_at": community.created_at.isoformat() if community.created_at else None,
    }


def _format_saga(saga) -> dict:
    """Format a saga node — name + group + dates. Member episodes are
    fetched via the saga's HAS_EPISODE edges when needed; inspect
    surfaces the saga itself, not its full membership."""
    return {
        "type": "saga",
        "uuid": saga.uuid,
        "name": saga.name,
        "group_id": saga.group_id,
        "created_at": saga.created_at.isoformat() if saga.created_at else None,
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
