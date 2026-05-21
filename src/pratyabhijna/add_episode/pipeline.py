"""Orchestrator for the in-house add_episode pipeline.

Sequences Stages 0 through 5 and emits INFO logging at each boundary.
The orchestrator is intentionally narrative — every meaningful decision
is logged at INFO so an operator reading the file knows what happened
without dropping to DEBUG.
"""

from __future__ import annotations

import asyncio
import time
import uuid as uuid_module
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from pratyabhijna.add_episode.extract import extract
from pratyabhijna.add_episode.hash import episode_hash
from pratyabhijna.add_episode.persist import (
    persist_edges_and_episode,
    persist_nodes_and_saga,
)
from pratyabhijna.add_episode.reconcile import (
    embed_all,
    fetch_edge_candidates,
    fetch_node_candidates,
    reconcile_edges,
    reconcile_nodes,
)
from pratyabhijna.log import get_logger

_log = get_logger(__name__)


@dataclass
class AddEpisodeResult:
    """Outcome of one add_episode invocation.

    Attributes:
        episode_uuid: UUID of the Episodic node (freshly created, or the
            existing one if Stage 0 short-circuited).
        nodes_created: Count of Entity nodes inserted in this run.
        nodes_updated: Count of existing Entity nodes whose attributes
            were merged with new values from this episode.
        edges_created: Count of EntityEdge relationships inserted.
        supersessions: Count of prior edges marked invalid by this run.
        short_circuited: True if Stage 0 found an existing Episodic node
            with the same hash and skipped all downstream stages.
    """

    episode_uuid: str
    nodes_created: int
    nodes_updated: int
    edges_created: int
    supersessions: int
    short_circuited: bool


@dataclass
class PrefetchResult:
    """Output of Stage 1 — what the extractor needs before it can run."""

    previous_episodes: list[EpisodicNode]
    saga_prior_uuid: str | None


# ---------------------------------------------------------------------------
# Stage 0 — idempotency
# ---------------------------------------------------------------------------


async def _check_idempotency(driver, *, group_id: str, episode_hash: str) -> str | None:
    """Stage 0 — return uuid of an existing Episodic with this hash, else None.

    Uses the composite index (Episodic.group_id, Episodic.episode_hash) created
    by PratyabhijnaService._ensure_episode_hash_index.
    """
    records, _, _ = await driver.execute_query(
        """
        MATCH (e:Episodic {group_id: $group_id, episode_hash: $episode_hash})
        RETURN e.uuid AS uuid
        LIMIT 1
        """,
        group_id=group_id,
        episode_hash=episode_hash,
        routing_="r",
    )
    return records[0]["uuid"] if records else None


# ---------------------------------------------------------------------------
# Stage 1 — pre-flight
# ---------------------------------------------------------------------------


async def _get_saga_latest_episode_uuid(
    driver, group_id: str, saga_name: str,
) -> str | None:
    records, _, _ = await driver.execute_query(
        """
        MATCH (s:Saga {group_id: $group_id, name: $saga_name})-[:HAS_EPISODE]->(e:Episodic)
        RETURN e.uuid AS uuid
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        group_id=group_id,
        saga_name=saga_name,
        routing_="r",
    )
    return records[0]["uuid"] if records else None


async def _prefetch(
    *,
    graphiti,
    group_id: str,
    reference_time: datetime,
    source: EpisodeType,
    previous_n: int,
    saga: str | None,
    saga_previous_episode_uuid: str | None,
) -> PrefetchResult:
    async def _episodes() -> list[EpisodicNode]:
        return await graphiti.retrieve_episodes(
            reference_time,
            last_n=previous_n,
            group_ids=[group_id],
            source=source,
        )

    async def _saga_prior() -> str | None:
        if not saga:
            return None
        if saga_previous_episode_uuid:
            return saga_previous_episode_uuid
        return await _get_saga_latest_episode_uuid(graphiti.driver, group_id, saga)

    episodes, saga_prior = await asyncio.gather(_episodes(), _saga_prior())
    return PrefetchResult(previous_episodes=episodes, saga_prior_uuid=saga_prior)


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


async def add_episode(
    service,
    *,
    name: str,
    episode_body: str,
    source_description: str,
    reference_time: datetime,
    source: EpisodeType = EpisodeType.message,
    group_id: str,
    saga: str | None = None,
    saga_previous_episode_uuid: str | None = None,
    custom_extraction_instructions: str | None = None,
) -> AddEpisodeResult:
    """In-house replacement for graphiti.add_episode.

    See doc/in-house-add-episode-design.md for the architecture and
    doc/in-house-add-episode-plan.md for the stage breakdown.
    """
    t_total = time.perf_counter()
    config = service.config
    driver = service._graphiti.driver
    llm_client = service._graphiti.llm_client
    embedder = service._graphiti.embedder

    _log.info(
        "add_episode start episode=%s group=%s source=%s body_chars=%d",
        name, group_id, source.value, len(episode_body),
    )

    # --- Stage 0: idempotency ---
    h = episode_hash(
        group_id=group_id,
        source=source,
        source_description=source_description,
        reference_time=reference_time,
        body=episode_body,
    )
    hit = await _check_idempotency(driver, group_id=group_id, episode_hash=h)
    if hit:
        _log.info(
            "add_episode stage=idempotency decision=existing_hit episode_uuid=%s", hit,
        )
        _log.info(
            "add_episode complete episode_uuid=%s total_latency_ms=%.0f llm_calls=0 embed_batches=0",
            hit, (time.perf_counter() - t_total) * 1000,
        )
        return AddEpisodeResult(
            episode_uuid=hit, nodes_created=0, nodes_updated=0,
            edges_created=0, supersessions=0, short_circuited=True,
        )
    _log.info("add_episode stage=idempotency decision=new")

    # --- Stage 1: prefetch ---
    prefetch = await _prefetch(
        graphiti=service._graphiti,
        group_id=group_id,
        reference_time=reference_time,
        source=source,
        previous_n=config.add_episode.previous_episodes_n,
        saga=saga,
        saga_previous_episode_uuid=saga_previous_episode_uuid,
    )
    _log.info(
        "add_episode stage=prefetch previous_episodes=%d saga=%s saga_prior=%s",
        len(prefetch.previous_episodes), saga or "none",
        prefetch.saga_prior_uuid or "none",
    )

    # --- Stage 2: extract ---
    t = time.perf_counter()
    extracted = await extract(
        llm_client=llm_client,
        episode_name=name,
        source=source,
        source_description=source_description,
        reference_time=reference_time,
        body=episode_body,
        previous_episodes=prefetch.previous_episodes,
        custom_instructions=custom_extraction_instructions,
    )
    extract_ms = (time.perf_counter() - t) * 1000
    type_counts = Counter(n.type for n in extracted.nodes)
    edge_name_counts = Counter(e.name for e in extracted.edges)
    _log.info(
        "add_episode stage=extract llm_calls=1 latency_ms=%.0f nodes=%d edges=%d",
        extract_ms, len(extracted.nodes), len(extracted.edges),
    )
    _log.info(
        "add_episode stage=extract types nodes=%s edges=%s",
        ",".join(f"{k}:{v}" for k, v in sorted(type_counts.items())) or "(none)",
        ",".join(f"{k}:{v}" for k, v in sorted(edge_name_counts.items())) or "(none)",
    )

    if not extracted.nodes and not extracted.edges:
        # Nothing to reconcile or persist — still create an Episodic for the
        # historical record and to honor idempotency.
        return await _short_persist_empty_episode(
            driver=driver, name=name, group_id=group_id,
            source=source, source_description=source_description,
            episode_body=episode_body, reference_time=reference_time,
            episode_hash=h, t_total=t_total,
        )

    # --- Stage 3a (embed + node candidates, in parallel) ---
    t = time.perf_counter()
    embed_task = asyncio.create_task(embed_all(embedder, extracted))
    node_emb, edge_emb = await embed_task
    node_candidates = await fetch_node_candidates(
        driver, group_id, extracted,
        node_embeddings=node_emb,
        k=config.add_episode.candidate_k,
    )
    embed_total = len(node_emb) + len(edge_emb)
    embed_batches = (embed_total + 127) // 128 if embed_total else 0
    _log.info(
        "add_episode stage=embed batches=%d texts=%d latency_ms=%.0f",
        embed_batches, embed_total, (time.perf_counter() - t) * 1000,
    )
    _log.info(
        "add_episode stage=fetch_node_candidates total_candidates=%d max_per_node=%d",
        sum(len(c) for c in node_candidates),
        max((len(c) for c in node_candidates), default=0),
    )

    # --- Stage 4a: reconcile nodes ---
    t = time.perf_counter()
    node_decisions = await reconcile_nodes(
        llm_client=llm_client, extracted=extracted, candidates=node_candidates,
    )
    existing_count = sum(1 for d in node_decisions.node_decisions if d.decision == "existing")
    new_count = sum(1 for d in node_decisions.node_decisions if d.decision == "new")
    attr_updates = sum(1 for d in node_decisions.node_decisions if d.attribute_updates)
    _log.info(
        "add_episode stage=reconcile_nodes llm_calls=1 latency_ms=%.0f existing=%d new=%d attribute_updates=%d",
        (time.perf_counter() - t) * 1000, existing_count, new_count, attr_updates,
    )

    # Materialize node objects + build idx → resolved-uuid map.
    now = datetime.now(timezone.utc)
    node_resolution: dict[int, str] = {}
    new_node_objects: list[EntityNode] = []
    updated_node_objects: list[tuple[EntityNode, dict]] = []
    for decision in node_decisions.node_decisions:
        ext = extracted.nodes[decision.extracted_idx]
        if decision.decision == "existing":
            node_resolution[decision.extracted_idx] = decision.existing_uuid
            if decision.attribute_updates:
                existing = await EntityNode.get_by_uuid(driver, decision.existing_uuid)
                updated_node_objects.append((existing, decision.attribute_updates))
        else:
            new_uuid = str(uuid_module.uuid4())
            node_resolution[decision.extracted_idx] = new_uuid
            new_node_objects.append(EntityNode(
                uuid=new_uuid,
                name=ext.name,
                group_id=group_id,
                labels=["Entity", ext.type],
                created_at=now,
                summary="",
                name_embedding=node_emb[decision.extracted_idx] if node_emb else None,
                attributes=ext.attributes,
            ))

    # --- Stage 3b: edge candidates (now that endpoints are known) ---
    edge_decisions = None
    new_edge_objects: list[EntityEdge] = []
    supersedes_uuids: list[str] = []
    ee_existing = ee_new = ee_super = 0

    if extracted.edges:
        t = time.perf_counter()
        edge_candidates = await fetch_edge_candidates(
            driver, group_id, extracted,
            node_resolution=node_resolution,
            edge_embeddings=edge_emb,
            k=config.add_episode.candidate_k,
        )
        _log.info(
            "add_episode stage=fetch_edge_candidates total_candidates=%d max_per_edge=%d",
            sum(len(c) for c in edge_candidates),
            max((len(c) for c in edge_candidates), default=0),
        )

        # --- Stage 4b: reconcile edges ---
        t = time.perf_counter()
        edge_decisions = await reconcile_edges(
            llm_client=llm_client, extracted=extracted, candidates=edge_candidates,
        )
        ee_existing = sum(1 for d in edge_decisions.edge_decisions if d.decision == "existing")
        ee_new = sum(1 for d in edge_decisions.edge_decisions if d.decision == "new")
        ee_super = sum(1 for d in edge_decisions.edge_decisions if d.decision == "supersedes")
        _log.info(
            "add_episode stage=reconcile_edges llm_calls=1 latency_ms=%.0f existing=%d new=%d supersedes=%d",
            (time.perf_counter() - t) * 1000, ee_existing, ee_new, ee_super,
        )

        # Materialize edge objects.
        for decision in edge_decisions.edge_decisions:
            ext = extracted.edges[decision.extracted_idx]
            if decision.decision == "existing":
                continue
            if decision.decision == "supersedes":
                supersedes_uuids.append(decision.existing_uuid)
            new_edge_objects.append(EntityEdge(
                source_node_uuid=node_resolution[ext.source_idx],
                target_node_uuid=node_resolution[ext.target_idx],
                name=ext.name,
                fact=ext.fact,
                group_id=group_id,
                created_at=now,
                valid_at=reference_time,
                fact_embedding=edge_emb[decision.extracted_idx] if edge_emb else None,
                episodes=[],
            ))

    # --- Stage 5: persist ---
    t = time.perf_counter()
    episode_node = EpisodicNode(
        name=name,
        group_id=group_id,
        labels=[],
        source=source,
        content=episode_body,
        source_description=source_description,
        created_at=now,
        valid_at=reference_time,
    )

    saga_node = await persist_nodes_and_saga(
        driver,
        new_nodes=new_node_objects,
        updated_nodes=updated_node_objects,
        saga_name=saga,
        group_id=group_id,
    )

    touched_uuids = list(node_resolution.values())
    await persist_edges_and_episode(
        driver,
        new_edges=new_edge_objects,
        supersedes_uuids=supersedes_uuids,
        episode=episode_node,
        episode_hash=h,
        touched_node_uuids=touched_uuids,
        saga=saga_node,
        saga_prior_episode_uuid=prefetch.saga_prior_uuid,
    )
    _log.info(
        "add_episode stage=persist nodes_created=%d nodes_updated=%d edges_created=%d "
        "supersessions=%d mentions=%d latency_ms=%.0f",
        len(new_node_objects), len(updated_node_objects),
        len(new_edge_objects), len(supersedes_uuids), len(touched_uuids),
        (time.perf_counter() - t) * 1000,
    )

    llm_calls = 2 + (1 if extracted.edges else 0)  # extract + reconcile_nodes (+ reconcile_edges)
    _log.info(
        "add_episode complete episode_uuid=%s total_latency_ms=%.0f llm_calls=%d embed_batches=%d",
        episode_node.uuid, (time.perf_counter() - t_total) * 1000,
        llm_calls, embed_batches,
    )

    return AddEpisodeResult(
        episode_uuid=episode_node.uuid,
        nodes_created=len(new_node_objects),
        nodes_updated=len(updated_node_objects),
        edges_created=len(new_edge_objects),
        supersessions=len(supersedes_uuids),
        short_circuited=False,
    )


async def _short_persist_empty_episode(
    *,
    driver,
    name: str,
    group_id: str,
    source: EpisodeType,
    source_description: str,
    episode_body: str,
    reference_time: datetime,
    episode_hash: str,
    t_total: float,
) -> AddEpisodeResult:
    """Persist an Episodic with zero extracted content.

    Keeps the hash idempotency contract: a re-queue of the same body will
    Stage-0-hit and skip re-extraction even when extraction returned nothing.
    """
    now = datetime.now(timezone.utc)
    episode_node = EpisodicNode(
        name=name,
        group_id=group_id,
        labels=[],
        source=source,
        content=episode_body,
        source_description=source_description,
        created_at=now,
        valid_at=reference_time,
    )
    await episode_node.save(driver)
    await driver.execute_query(
        "MATCH (e:Episodic {uuid: $u}) SET e.episode_hash = $h",
        u=episode_node.uuid, h=episode_hash,
    )
    _log.info(
        "add_episode complete episode_uuid=%s total_latency_ms=%.0f llm_calls=1 embed_batches=0 "
        "(no entities or edges extracted)",
        episode_node.uuid, (time.perf_counter() - t_total) * 1000,
    )
    return AddEpisodeResult(
        episode_uuid=episode_node.uuid,
        nodes_created=0, nodes_updated=0, edges_created=0,
        supersessions=0, short_circuited=False,
    )
