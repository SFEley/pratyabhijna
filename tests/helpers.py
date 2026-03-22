"""Shared test helpers for Pratyabhijna memory server tests."""

import asyncio
from datetime import datetime, timezone

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode, EpisodeType


async def wait_for(condition, timeout=2.0, interval=0.02):
    """Poll ``condition()`` (sync or async) until truthy, or raise on timeout.

    Useful for waiting on background queue processing in tests.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    raise TimeoutError("Condition not met within timeout")


def make_entity_node(
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


def make_entity_edge(
    uuid="edge-1",
    name="values",
    fact="Serah values directness",
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


def make_subject_node(
    name="Vesper",
    uuid="subject-uuid",
    synthesis_text=None,
    rebuilt_at=None,
    **extra_attrs,
):
    """Create a subject Person node with optional synthesis metadata.

    Used by identity synthesis tests. The name parameter allows testing
    with different configured subject names.
    """
    attrs = {"person_type": "AI"}
    if synthesis_text is not None:
        attrs["notes"] = synthesis_text
    if rebuilt_at is not None:
        attrs["synthesis_rebuilt_at"] = rebuilt_at.isoformat()
    attrs.update(extra_attrs)
    return make_entity_node(
        uuid=uuid,
        name=name,
        labels=["Person"],
        summary="An AI identity",
        attributes=attrs,
    )


def make_episodic_node(
    uuid="ep-1",
    content="Serah said she values directness.",
    source_description="vesper",
    created_at=None,
):
    return EpisodicNode(
        uuid=uuid,
        name=f"episode:{uuid}",
        group_id="default",
        labels=["Episodic"],
        created_at=created_at or datetime(2026, 3, 15, tzinfo=timezone.utc),
        source=EpisodeType.text,
        source_description=source_description,
        content=content,
        valid_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        entity_edges=[],
    )
