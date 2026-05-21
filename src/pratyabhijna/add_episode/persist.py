"""Stage 5 — graph writes.

Two phases, ordered (5a then 5b) but parallelized within each phase:
- 5a: new entity nodes, attribute updates on existing nodes, saga node creation.
- 5b: entity edges, supersession invalidation, episode, MENTIONS edges, saga chain.

The phase ordering guarantees a Phase 5b failure can never leave dangling
edges pointing at missing endpoints — the worst case is orphaned nodes,
which a retry adopts via dedup.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode, SagaNode


async def persist_nodes_and_saga(
    driver,
    *,
    new_nodes: list[EntityNode],
    updated_nodes: list[tuple[EntityNode, dict[str, Any]]],
    saga_name: str | None,
    group_id: str,
) -> SagaNode | None:
    """Phase 5a — save new nodes, apply attribute merges, ensure saga.

    Returns the SagaNode (existing or freshly created) when saga_name is set,
    else None.
    """
    save_tasks = [node.save(driver) for node in new_nodes]
    for node, updates in updated_nodes:
        merged = dict(node.attributes or {})
        merged.update(updates)
        node.attributes = merged
        save_tasks.append(node.save(driver))

    saga_task = _ensure_saga(driver, group_id, saga_name) if saga_name else None

    if save_tasks:
        await asyncio.gather(*save_tasks)
    if saga_task is None:
        return None
    return await saga_task


async def _ensure_saga(driver, group_id: str, name: str) -> SagaNode:
    records, _, _ = await driver.execute_query(
        "MATCH (s:Saga {group_id: $g, name: $n}) RETURN s.uuid AS uuid LIMIT 1",
        g=group_id, n=name, routing_="r",
    )
    if records:
        return await SagaNode.get_by_uuid(driver, records[0]["uuid"])
    saga = SagaNode(
        name=name,
        group_id=group_id,
        labels=["Saga"],
        created_at=datetime.now(),
    )
    await saga.save(driver)
    return saga


async def persist_edges_and_episode(
    driver,
    *,
    new_edges: list[EntityEdge],
    supersedes_uuids: list[str],
    episode: EpisodicNode,
    episode_hash: str,
    touched_node_uuids: list[str],
    saga: SagaNode | None,
    saga_prior_episode_uuid: str | None,
) -> None:
    """Phase 5b — edges, supersession, episode, MENTIONS, saga chain.

    `episode_hash` is set after `episode.save()` because graphiti's EpisodicNode
    schema doesn't include the hash property and the underlying save query
    is generated from the model. A trailing SET attaches the hash; that's
    what the Stage 0 idempotency MATCH reads on future runs.
    """
    edge_tasks = [edge.save(driver) for edge in new_edges]

    super_task = None
    if supersedes_uuids:
        super_task = driver.execute_query(
            """
            MATCH ()-[r:RELATES_TO]-()
            WHERE r.uuid IN $uuids
              AND r.invalid_at IS NULL
              AND r.group_id = $g
            SET r.invalid_at = $valid_at
            """,
            uuids=supersedes_uuids,
            g=episode.group_id,
            valid_at=episode.valid_at,
        )

    awaitable_tasks = list(edge_tasks)
    if super_task is not None:
        awaitable_tasks.append(super_task)
    if awaitable_tasks:
        await asyncio.gather(*awaitable_tasks)

    await episode.save(driver)
    await driver.execute_query(
        "MATCH (e:Episodic {uuid: $u}) SET e.episode_hash = $h",
        u=episode.uuid, h=episode_hash,
    )

    final_tasks = []
    if touched_node_uuids:
        final_tasks.append(driver.execute_query(
            """
            MATCH (e:Episodic {uuid: $ep})
            UNWIND $node_uuids AS nuuid
            MATCH (n:Entity {uuid: nuuid})
            MERGE (e)-[m:MENTIONS]->(n)
            SET m.group_id = $g, m.created_at = $created_at
            """,
            ep=episode.uuid,
            node_uuids=touched_node_uuids,
            g=episode.group_id,
            created_at=episode.created_at,
        ))

    if saga is not None:
        final_tasks.append(driver.execute_query(
            """
            MATCH (s:Saga {uuid: $saga}), (e:Episodic {uuid: $ep})
            MERGE (s)-[:HAS_EPISODE]->(e)
            """,
            saga=saga.uuid, ep=episode.uuid,
        ))
        if saga_prior_episode_uuid:
            final_tasks.append(driver.execute_query(
                """
                MATCH (p:Episodic {uuid: $prev}), (e:Episodic {uuid: $ep})
                MERGE (p)-[:NEXT_EPISODE]->(e)
                """,
                prev=saga_prior_episode_uuid, ep=episode.uuid,
            ))

    if final_tasks:
        await asyncio.gather(*final_tasks)
