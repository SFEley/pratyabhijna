"""Reconcile stages — candidate fetch and LLM-based dedup decisions.

Stages handled here:
- 3a-i: batched embedding of extracted node names + edge facts
- 3a-ii: node candidate fetch (name-similarity + embedding-similarity)
- 4a: reconcile-nodes LLM call
- 3b: edge candidate fetch (resolved-endpoint + fact-embedding)
- 4b: reconcile-edges LLM call
"""

from __future__ import annotations

from typing import Any

from graphiti_core.llm_client import LLMClient
from graphiti_core.prompts.models import Message

from pratyabhijna.add_episode.prompts import (
    build_reconcile_edges_prompt,
    build_reconcile_nodes_prompt,
)
from pratyabhijna.add_episode.schemas import (
    ExtractResponse,
    ReconcileEdgesResponse,
    ReconcileNodesResponse,
)


# Voyage accepts up to 128 texts per embed call.
VOYAGE_BATCH_LIMIT = 128
# Conservative min-score for embedding-based candidate inclusion. Below this,
# the embedding similarity is too low to be useful as a candidate hint.
_MIN_EMBEDDING_SCORE = 0.55


# ---------------------------------------------------------------------------
# 3a-i — batched Voyage embeddings
# ---------------------------------------------------------------------------


async def embed_all(
    embedder, extracted: ExtractResponse,
) -> tuple[list[list[float]], list[list[float]]]:
    """Embed every extracted node name and edge fact in batched calls.

    Returns (node_embeddings, edge_embeddings) preserving extracted order.
    Splits into VOYAGE_BATCH_LIMIT-sized batches if needed.
    """
    node_texts = [n.name for n in extracted.nodes]
    edge_texts = [e.fact for e in extracted.edges]
    all_texts = node_texts + edge_texts

    if not all_texts:
        return [], []

    embeddings: list[list[float]] = []
    for i in range(0, len(all_texts), VOYAGE_BATCH_LIMIT):
        batch = all_texts[i : i + VOYAGE_BATCH_LIMIT]
        result = await embedder.create_batch(batch)
        embeddings.extend(result)

    split = len(node_texts)
    return embeddings[:split], embeddings[split:]


# ---------------------------------------------------------------------------
# 3a-ii — node candidate fetch
# ---------------------------------------------------------------------------


async def fetch_node_candidates(
    driver,
    group_id: str,
    extracted: ExtractResponse,
    *,
    node_embeddings: list[list[float]],
    k: int = 5,
) -> list[list[dict[str, Any]]]:
    """Per-extracted-node candidate sets, unioned from name + embedding sources.

    Returns a list indexed by extracted-node idx; each entry is a list of
    candidate dicts (uuid, name, summary, attributes, labels) deduped by uuid.
    """
    name_results = await _name_similar_candidates(driver, group_id, extracted, k=k)
    embed_results = await _embedding_similar_node_candidates(
        driver, group_id, node_embeddings, k=k,
    )

    merged: list[list[dict[str, Any]]] = []
    for name_hits, embed_hits in zip(name_results, embed_results, strict=True):
        seen: dict[str, dict[str, Any]] = {}
        for c in [*name_hits, *embed_hits]:
            seen.setdefault(c["uuid"], c)
        merged.append(list(seen.values()))
    return merged


async def _name_similar_candidates(
    driver, group_id: str, extracted: ExtractResponse, *, k: int,
) -> list[list[dict[str, Any]]]:
    """Per-extracted-node fuzzy name match — case-insensitive substring + equality.

    The full graphiti dedup_helpers MinHash/LSH path would be marginally better
    on long noisy names; for Vesper's vocabulary (mostly short proper nouns)
    the substring match catches almost everything name-similarity should catch.
    """
    out: list[list[dict[str, Any]]] = []
    for node in extracted.nodes:
        needle = node.name.strip()
        if not needle:
            out.append([])
            continue
        records, _, _ = await driver.execute_query(
            """
            MATCH (n:Entity {group_id: $group_id})
            WHERE toLower(n.name) = toLower($exact)
               OR toLower(n.name) CONTAINS toLower($substr)
            RETURN n.uuid AS uuid, n.name AS name,
                   n.summary AS summary, n.attributes AS attributes,
                   labels(n) AS labels
            LIMIT $k
            """,
            group_id=group_id,
            exact=needle,
            substr=needle[: min(len(needle), 64)],
            k=k,
            routing_="r",
        )
        out.append([dict(r) for r in records])
    return out


async def _embedding_similar_node_candidates(
    driver,
    group_id: str,
    embeddings: list[list[float]],
    *,
    k: int,
) -> list[list[dict[str, Any]]]:
    """Per-extracted-node embedding-similarity top-K via inline cosine.

    Uses graphiti's vector.similarity.cosine pattern (no vector index needed);
    O(N) scan over Entity nodes in the group. Acceptable at current scale.
    """
    out: list[list[dict[str, Any]]] = []
    for emb in embeddings:
        if not emb:
            out.append([])
            continue
        records, _, _ = await driver.execute_query(
            """
            MATCH (n:Entity {group_id: $group_id})
            WHERE n.name_embedding IS NOT NULL
            WITH n, vector.similarity.cosine(n.name_embedding, $emb) AS score
            WHERE score > $min_score
            RETURN n.uuid AS uuid, n.name AS name,
                   n.summary AS summary, n.attributes AS attributes,
                   labels(n) AS labels, score
            ORDER BY score DESC
            LIMIT $k
            """,
            group_id=group_id,
            emb=emb,
            min_score=_MIN_EMBEDDING_SCORE,
            k=k,
            routing_="r",
        )
        out.append([dict(r) for r in records])
    return out


# ---------------------------------------------------------------------------
# 4a — reconcile nodes
# ---------------------------------------------------------------------------


async def reconcile_nodes(
    *,
    llm_client: LLMClient,
    extracted: ExtractResponse,
    candidates: list[list[dict[str, Any]]],
) -> ReconcileNodesResponse:
    """Single LLM call that decides existing-vs-new for each extracted node."""
    nodes_serialized = [
        {"idx": n.idx, "name": n.name, "type": n.type, "attributes": n.attributes}
        for n in extracted.nodes
    ]
    user_prompt = build_reconcile_nodes_prompt(
        extracted_nodes=nodes_serialized, candidates=candidates,
    )
    messages = [
        Message(
            role="system",
            content="You are a deduplication assistant for a knowledge graph.",
        ),
        Message(role="user", content=user_prompt),
    ]
    raw = await llm_client.generate_response(
        messages,
        response_model=ReconcileNodesResponse,
        prompt_name="pratyabhijna.add_episode.reconcile_nodes",
    )
    response = ReconcileNodesResponse(**raw) if isinstance(raw, dict) else raw

    expected = set(range(len(extracted.nodes)))
    seen = {d.extracted_idx for d in response.node_decisions}
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise ValueError(
            f"reconcile_nodes missing decision for extracted idx {missing}, "
            f"unexpected idx {extra}"
        )
    return response


# ---------------------------------------------------------------------------
# 3b — edge candidate fetch
# ---------------------------------------------------------------------------


async def fetch_edge_candidates(
    driver,
    group_id: str,
    extracted: ExtractResponse,
    *,
    node_resolution: dict[int, str],
    edge_embeddings: list[list[float]],
    k: int = 5,
) -> list[list[dict[str, Any]]]:
    """Per-extracted-edge candidate sets, unioned from endpoint + embedding sources.

    Endpoint matches are the high-precision signal (same subject + object).
    Embedding matches are the recall safety net (similar fact, possibly
    different endpoints). Both lists are deduped by edge uuid.
    """
    endpoint_results = await _endpoint_edge_candidates(
        driver, group_id, extracted, node_resolution=node_resolution,
    )
    embed_results = await _embedding_similar_edge_candidates(
        driver, group_id, edge_embeddings, endpoints=list(node_resolution.values()), k=k,
    )

    merged: list[list[dict[str, Any]]] = []
    for endpoint_hits, embed_hits in zip(endpoint_results, embed_results, strict=True):
        seen: dict[str, dict[str, Any]] = {}
        for c in [*endpoint_hits, *embed_hits]:
            seen.setdefault(c["uuid"], c)
        merged.append(list(seen.values()))
    return merged


async def _endpoint_edge_candidates(
    driver,
    group_id: str,
    extracted: ExtractResponse,
    *,
    node_resolution: dict[int, str],
) -> list[list[dict[str, Any]]]:
    """Edges between the resolved endpoint pair (either direction)."""
    out: list[list[dict[str, Any]]] = []
    for edge in extracted.edges:
        src_uuid = node_resolution[edge.source_idx]
        tgt_uuid = node_resolution[edge.target_idx]
        records, _, _ = await driver.execute_query(
            """
            MATCH (s:Entity {uuid: $src})-[r:RELATES_TO]-(t:Entity {uuid: $tgt})
            WHERE r.group_id = $group_id
            RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact,
                   startNode(r).uuid AS source_uuid,
                   endNode(r).uuid AS target_uuid,
                   r.valid_at AS valid_at, r.invalid_at AS invalid_at
            """,
            src=src_uuid,
            tgt=tgt_uuid,
            group_id=group_id,
            routing_="r",
        )
        out.append([dict(r) for r in records])
    return out


async def _embedding_similar_edge_candidates(
    driver,
    group_id: str,
    embeddings: list[list[float]],
    *,
    endpoints: list[str],
    k: int,
) -> list[list[dict[str, Any]]]:
    """Edges with fact-embedding similarity, restricted to touched endpoints."""
    out: list[list[dict[str, Any]]] = []
    for emb in embeddings:
        if not emb or not endpoints:
            out.append([])
            continue
        records, _, _ = await driver.execute_query(
            """
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE r.group_id = $group_id
              AND r.fact_embedding IS NOT NULL
              AND (a.uuid IN $endpoints OR b.uuid IN $endpoints)
            WITH r, a, b,
                 vector.similarity.cosine(r.fact_embedding, $emb) AS score
            WHERE score > $min_score
            RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact,
                   a.uuid AS source_uuid, b.uuid AS target_uuid,
                   r.valid_at AS valid_at, r.invalid_at AS invalid_at, score
            ORDER BY score DESC
            LIMIT $k
            """,
            group_id=group_id,
            emb=emb,
            endpoints=endpoints,
            min_score=_MIN_EMBEDDING_SCORE,
            k=k,
            routing_="r",
        )
        out.append([dict(r) for r in records])
    return out


# ---------------------------------------------------------------------------
# 4b — reconcile edges
# ---------------------------------------------------------------------------


async def reconcile_edges(
    *,
    llm_client: LLMClient,
    extracted: ExtractResponse,
    candidates: list[list[dict[str, Any]]],
) -> ReconcileEdgesResponse:
    """Single LLM call for new/existing/supersedes decisions per edge."""
    edges_serialized = [
        {"idx": e.idx, "name": e.name, "fact": e.fact}
        for e in extracted.edges
    ]
    user_prompt = build_reconcile_edges_prompt(
        extracted_edges=edges_serialized, candidates=candidates,
    )
    messages = [
        Message(
            role="system",
            content=(
                "You are a deduplication and contradiction-detection assistant "
                "for a knowledge graph."
            ),
        ),
        Message(role="user", content=user_prompt),
    ]
    raw = await llm_client.generate_response(
        messages,
        response_model=ReconcileEdgesResponse,
        prompt_name="pratyabhijna.add_episode.reconcile_edges",
    )
    response = ReconcileEdgesResponse(**raw) if isinstance(raw, dict) else raw

    expected = set(range(len(extracted.edges)))
    seen = {d.extracted_idx for d in response.edge_decisions}
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise ValueError(
            f"reconcile_edges missing decision for extracted idx {missing}, "
            f"unexpected idx {extra}"
        )
    return response
