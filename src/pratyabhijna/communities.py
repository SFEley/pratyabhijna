"""In-house community detection for Pratyabhijna.

Replaces Graphiti's build_communities with a Neo4j-only implementation that
fixes four known bugs: O(n) projection queries, infinite-oscillation label
propagation, O(n·log m) LLM summarization calls, and an add_episode unpack
failure.

Algorithm: async label propagation (Raghavan, Albert, Kumara 2007) with
deterministic tie-breaking and oscillation detection. LLM cost is bounded
by sampling the top-k most representative members per community.
"""

import asyncio
import random
from collections import defaultdict, deque

from graphiti_core.edges import CommunityEdge
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import CommunityNode, EntityNode
from graphiti_core.prompts.models import Message
from graphiti_core.utils.datetime_utils import utc_now
from graphiti_core.utils.maintenance.community_operations import (
    MAX_SUMMARY_CHARS,
    Neighbor,
    build_community_edges,
    remove_communities,
    summarize_pair,
    truncate_at_sentence,
)
from pydantic import BaseModel, Field

from pratyabhijna.log import get_logger

_log = get_logger(__name__)

DEFAULT_SAMPLE_SIZE = 10
DEFAULT_MIN_COMMUNITY_SIZE = 4
MAX_CONCURRENCY = 10
OSCILLATION_WINDOW = 8
RNG_SEED = 42


# ---------------------------------------------------------------------------
# Response model and prompt for combined summary + name generation
# ---------------------------------------------------------------------------


class _CommunityResult(BaseModel):
    summary: str = Field(
        ...,
        description=(
            f"Concise factual summary under {MAX_SUMMARY_CHARS} characters. "
            "Preserve all key names, dates, roles, and facts."
        ),
    )
    name: str = Field(
        ...,
        description=(
            "Short 2-5 word topic label in title case, no punctuation. "
            "Examples: 'Mycorrhizal Network Research', 'Monteverdi 1610 Vespers', "
            "'Physarum Habituation Experiments'."
        ),
    )


def _finalize_prompt(raw_summary: str) -> list[Message]:
    return [
        Message(
            role="system",
            content="You are a helpful assistant that cleans up community summaries and produces concise topic labels.",
        ),
        Message(
            role="user",
            content=(
                f"Given the following community summary, produce:\n"
                f"1. A cleaned factual summary under {MAX_SUMMARY_CHARS} characters.\n"
                f"2. A short 2-5 word topic label in title case with no punctuation "
                f"(e.g. 'Mycorrhizal Network Research', 'Monteverdi 1610 Vespers').\n\n"
                f"Summary:\n{raw_summary}"
            ),
        ),
    ]


async def _finalize_community(
    llm_client: LLMClient, raw_summary: str
) -> tuple[str, str]:
    """Single LLM call returning (summary, name) for a community."""
    response = await llm_client.generate_response(
        _finalize_prompt(raw_summary),
        response_model=_CommunityResult,
        prompt_name="pratyabhijna.finalize_community",
    )
    summary = truncate_at_sentence(response.get("summary", raw_summary), MAX_SUMMARY_CHARS)
    name = response.get("name", "") or "community"
    return summary, name


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


async def _build_projection(
    driver, group_id: str
) -> dict[str, list[Neighbor]]:
    """Fetch RELATES_TO adjacency for all entities in a group in one query."""
    nodes = await EntityNode.get_by_group_ids(driver, [group_id])

    edge_records, _, _ = await driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})-[e:RELATES_TO]-(m:Entity {group_id: $group_id})
        RETURN n.uuid AS src_uuid, m.uuid AS tgt_uuid, count(e) AS edge_count
        """,
        group_id=group_id,
        routing_="r",
    )

    # Seed isolated nodes so they appear in the projection with empty neighbor lists.
    projection: dict[str, list[Neighbor]] = {node.uuid: [] for node in nodes}
    for record in edge_records:
        src = record["src_uuid"]
        if src in projection:
            projection[src].append(
                Neighbor(node_uuid=record["tgt_uuid"], edge_count=record["edge_count"])
            )

    _log.debug(
        "community projection: group=%s nodes=%d edge_records=%d",
        group_id,
        len(nodes),
        len(edge_records),
    )
    return projection


# ---------------------------------------------------------------------------
# Label propagation
# ---------------------------------------------------------------------------


def label_propagation(projection: dict[str, list[Neighbor]]) -> list[list[str]]:
    """Async label propagation (Raghavan et al. 2007) with oscillation guard.

    Each node starts in its own community. On each pass, nodes are visited in
    a fresh random order and assigned to the plurality-weight community of
    their current neighbors (in-place reads, not a frozen snapshot). This
    breaks the ping-pong oscillation that the synchronous batch-update form
    suffers on hub-heavy graphs.

    Convergence is guaranteed on undirected graphs; a state-hash window
    catches any pathological cycle that survives the async form.
    """
    if not projection:
        return []

    community_map: dict[str, int] = {uuid: i for i, uuid in enumerate(projection)}
    node_order = list(projection)
    rng = random.Random(RNG_SEED)
    recent_hashes: deque[int] = deque(maxlen=OSCILLATION_WINDOW)

    while True:
        rng.shuffle(node_order)
        no_change = True

        for uuid in node_order:
            neighbors = projection[uuid]
            if not neighbors:
                continue

            curr = community_map[uuid]
            tally: dict[int, int] = defaultdict(int)
            for nb in neighbors:
                tally[community_map[nb.node_uuid]] += nb.edge_count

            if not tally:
                continue

            best_community, best_count = max(tally.items(), key=lambda kv: (kv[1], kv[0]))
            curr_support = tally.get(curr, 0)

            if best_count > curr_support:
                new = best_community
            elif best_count == curr_support and best_community > curr:
                new = best_community
            else:
                new = curr

            if new != curr:
                community_map[uuid] = new
                no_change = False

        if no_change:
            break

        state_hash = hash(frozenset(community_map.items()))
        if state_hash in recent_hashes:
            _log.warning("label_propagation oscillation detected — using current partition")
            break
        recent_hashes.append(state_hash)

    clusters: dict[int, list[str]] = defaultdict(list)
    for uuid, community in community_map.items():
        clusters[community].append(uuid)
    return list(clusters.values())


# ---------------------------------------------------------------------------
# Small-cluster merging
# ---------------------------------------------------------------------------


def _merge_small_clusters(
    uuid_clusters: list[list[str]],
    projection: dict[str, list[Neighbor]],
    min_size: int,
) -> list[list[str]]:
    """Reassign nodes from sub-threshold clusters into their best-connected large cluster.

    Nodes with no edges into any large cluster are dropped (they receive no
    community membership). Returns only clusters that meet or exceed min_size.
    """
    if min_size <= 1:
        return uuid_clusters

    large = [c for c in uuid_clusters if len(c) >= min_size]
    small = [c for c in uuid_clusters if len(c) < min_size]

    if not small:
        return large

    # Build UUID → large-cluster-index map for fast lookup.
    node_to_large: dict[str, int] = {}
    for idx, cluster in enumerate(large):
        for uuid in cluster:
            node_to_large[uuid] = idx

    # Accumulate reassigned nodes per large cluster.
    additions: dict[int, list[str]] = defaultdict(list)
    for cluster in small:
        for uuid in cluster:
            tally: dict[int, int] = defaultdict(int)
            for nb in projection.get(uuid, []):
                dest = node_to_large.get(nb.node_uuid)
                if dest is not None:
                    tally[dest] += nb.edge_count
            if tally:
                best = max(tally, key=lambda k: (tally[k], k))
                additions[best].append(uuid)
            # else: no edges to any large cluster — node is dropped

    if not additions:
        return large

    result = [list(cluster) for cluster in large]
    for idx, nodes in additions.items():
        result[idx].extend(nodes)
    return result


# ---------------------------------------------------------------------------
# Member sampling
# ---------------------------------------------------------------------------


def _select_representative_members(
    cluster: list[EntityNode],
    projection: dict[str, list[Neighbor]] | None,
    sample_size: int,
) -> list[EntityNode]:
    """Return the top-k most representative members of a community cluster.

    Ranking: in-community weighted degree (desc), summary length (desc),
    name (desc) as a deterministic tie-breaker. Falls back to summary length
    when projection is absent. Returns the full cluster when len ≤ sample_size.
    """
    if len(cluster) <= sample_size:
        return cluster

    member_uuids = {m.uuid for m in cluster}

    def in_community_degree(entity: EntityNode) -> int:
        if not projection:
            return 0
        return sum(
            nb.edge_count
            for nb in projection.get(entity.uuid, [])
            if nb.node_uuid in member_uuids
        )

    return sorted(
        cluster,
        key=lambda e: (in_community_degree(e), len(e.summary or ""), e.name),
        reverse=True,
    )[:sample_size]


# ---------------------------------------------------------------------------
# Community node building
# ---------------------------------------------------------------------------


async def _build_community_node(
    llm_client: LLMClient,
    cluster: list[EntityNode],
    projection: dict[str, list[Neighbor]],
    sample_size: int,
) -> tuple[CommunityNode, list[CommunityEdge]]:
    """Build a CommunityNode and its HAS_MEMBER edges from a cluster of entities."""
    summary_members = _select_representative_members(cluster, projection, sample_size)
    summaries = [e.summary for e in summary_members]

    # Binary-merge summaries (Graphiti's existing pattern).
    length = len(summaries)
    while length > 1:
        odd_one_out: str | None = None
        if length % 2 == 1:
            odd_one_out = summaries.pop()
            length -= 1
        pairs = list(
            zip(summaries[: length // 2], summaries[length // 2 :], strict=False)
        )
        new_summaries = list(
            await asyncio.gather(
                *[summarize_pair(llm_client, (str(l), str(r))) for l, r in pairs]
            )
        )
        if odd_one_out is not None:
            new_summaries.append(odd_one_out)
        summaries = new_summaries
        length = len(summaries)

    raw = summaries[0] if summaries else ""
    if raw:
        summary, name = await _finalize_community(llm_client, raw)
    else:
        summary, name = "", "community"

    now = utc_now()
    node = CommunityNode(
        name=name,
        group_id=cluster[0].group_id,
        labels=["Community"],
        created_at=now,
        summary=summary,
    )
    edges = build_community_edges(cluster, node, now)

    _log.debug(
        "community built: uuid=%s members=%d summary_from=%d/%d",
        node.uuid,
        len(edges),
        len(summary_members),
        len(cluster),
    )
    return node, edges


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _save_community(
    driver, node: CommunityNode, edges: list[CommunityEdge]
) -> None:
    """Persist a community node and its HAS_MEMBER edges to Neo4j."""
    await driver.execute_query(
        """
        MERGE (c:Community {uuid: $uuid})
        SET c.name = $name,
            c.group_id = $group_id,
            c.summary = $summary,
            c.created_at = $created_at
        """,
        uuid=node.uuid,
        name=node.name,
        group_id=node.group_id,
        summary=node.summary,
        created_at=node.created_at,
        routing_="w",
    )
    for edge in edges:
        await driver.execute_query(
            """
            MATCH (c:Community {uuid: $community_uuid})
            MATCH (e:Entity {uuid: $entity_uuid})
            MERGE (c)-[r:HAS_MEMBER {uuid: $uuid}]->(e)
            SET r.group_id = $group_id,
                r.created_at = $created_at
            """,
            community_uuid=edge.source_node_uuid,
            entity_uuid=edge.target_node_uuid,
            uuid=edge.uuid,
            group_id=edge.group_id,
            created_at=edge.created_at,
            routing_="w",
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def build_communities(
    driver,
    llm_client: LLMClient,
    group_ids: list[str] | None,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    min_community_size: int = DEFAULT_MIN_COMMUNITY_SIZE,
) -> tuple[list[CommunityNode], list[CommunityEdge]]:
    """Cluster entities into communities and build a summary node for each.

    Clears all existing Community nodes first (full rebuild). Uses async label
    propagation with top-k member sampling to bound LLM cost. Sub-threshold
    clusters are merged into their best-connected large cluster.

    Args:
        driver: Neo4j driver (from graphiti._graphiti.driver).
        llm_client: LLM client for pair summarization and name generation.
        group_ids: Scope clustering to these group ids, or all if None.
        sample_size: Maximum number of members used for LLM summarization per
            community. Only the top-k by in-community weighted degree are used;
            all members still appear in HAS_MEMBER edges.
        min_community_size: Communities smaller than this are merged into their
            best-connected large cluster. Nodes with no large-cluster connections
            are dropped. Default 4.
    """
    await remove_communities(driver)

    effective_groups: list[str]
    if group_ids:
        effective_groups = group_ids
    else:
        records, _, _ = await driver.execute_query(
            "MATCH (n:Entity) RETURN DISTINCT n.group_id AS group_id"
        )
        effective_groups = [r["group_id"] for r in records if r["group_id"]]

    all_nodes: list[CommunityNode] = []
    all_edges: list[CommunityEdge] = []

    for group_id in effective_groups:
        projection = await _build_projection(driver, group_id)
        entity_count = len(projection)
        _log.info("community build start: group=%s entities=%d", group_id, entity_count)

        if entity_count == 0:
            _log.info(
                "community build complete: group=%s communities=0 edges=0", group_id
            )
            continue

        uuid_clusters = label_propagation(projection)
        uuid_clusters = _merge_small_clusters(uuid_clusters, projection, min_community_size)

        # Resolve UUID lists to EntityNode lists via a single fetch.
        all_entities = await EntityNode.get_by_group_ids(driver, [group_id])
        entity_by_uuid = {e.uuid: e for e in all_entities}

        clusters: list[list[EntityNode]] = []
        for uuid_list in uuid_clusters:
            members = [entity_by_uuid[u] for u in uuid_list if u in entity_by_uuid]
            if members:
                clusters.append(members)

        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def limited_build(cluster: list[EntityNode]) -> tuple[CommunityNode, list[CommunityEdge]]:
            async with semaphore:
                return await _build_community_node(llm_client, cluster, projection, sample_size)

        results = await asyncio.gather(*[limited_build(c) for c in clusters])

        group_nodes: list[CommunityNode] = []
        group_edges: list[CommunityEdge] = []
        for node, edges in results:
            await _save_community(driver, node, edges)
            group_nodes.append(node)
            group_edges.extend(edges)

        _log.info(
            "community build complete: group=%s communities=%d edges=%d",
            group_id,
            len(group_nodes),
            len(group_edges),
        )
        all_nodes.extend(group_nodes)
        all_edges.extend(group_edges)

    return all_nodes, all_edges
