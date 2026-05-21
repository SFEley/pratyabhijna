"""Tests for the reconcile stages — embeddings, candidate fetch, LLM calls."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pratyabhijna.add_episode.reconcile import (
    embed_all,
    fetch_edge_candidates,
    fetch_node_candidates,
    reconcile_edges,
    reconcile_nodes,
)
from pratyabhijna.add_episode.schemas import (
    EdgeDecision,
    ExtractedEdge,
    ExtractedNode,
    ExtractResponse,
    NodeDecision,
    ReconcileEdgesResponse,
    ReconcileNodesResponse,
)


def _nodes(n: int):
    return [
        ExtractedNode(idx=i, name=f"n{i}", type="Concept", attributes={})
        for i in range(n)
    ]


def _edge(idx=0, src=0, tgt=1, fact="rel"):
    return ExtractedEdge(idx=idx, source_idx=src, target_idx=tgt, name="rel", fact=fact)


# ---------------------------------------------------------------------------
# embed_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_all_single_batch():
    embedder = MagicMock()
    embedder.create_batch = AsyncMock(return_value=[[0.1] * 4, [0.2] * 4, [0.3] * 4])
    extracted = ExtractResponse(
        nodes=_nodes(2),
        edges=[_edge()],
    )
    node_emb, edge_emb = await embed_all(embedder, extracted)
    assert node_emb == [[0.1] * 4, [0.2] * 4]
    assert edge_emb == [[0.3] * 4]
    embedder.create_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_embed_all_splits_oversized_batches():
    embedder = MagicMock()
    embedder.create_batch = AsyncMock(side_effect=lambda texts: [[0.0] * 4] * len(texts))
    extracted = ExtractResponse(nodes=_nodes(130), edges=[])
    node_emb, edge_emb = await embed_all(embedder, extracted)
    assert len(node_emb) == 130
    assert len(edge_emb) == 0
    assert embedder.create_batch.await_count == 2  # 128 + 2


@pytest.mark.asyncio
async def test_embed_all_empty():
    embedder = MagicMock()
    embedder.create_batch = AsyncMock()
    extracted = ExtractResponse(nodes=[], edges=[])
    n, e = await embed_all(embedder, extracted)
    assert n == [] and e == []
    embedder.create_batch.assert_not_awaited()


# ---------------------------------------------------------------------------
# fetch_node_candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_node_candidates_union_dedups_by_uuid():
    """Same uuid in both name + embedding lists collapses to one entry."""
    driver = MagicMock()
    driver.execute_query = AsyncMock()

    # First call: name-similar. Second call: embedding-similar.
    driver.execute_query.side_effect = [
        ([{"uuid": "u1", "name": "Serah", "summary": None, "attributes": {}, "labels": ["Entity", "Person"]}], None, None),
        ([
            {"uuid": "u1", "name": "Serah", "summary": None, "attributes": {}, "labels": ["Entity", "Person"], "score": 0.9},
            {"uuid": "u2", "name": "Sara", "summary": None, "attributes": {}, "labels": ["Entity", "Person"], "score": 0.7},
        ], None, None),
    ]

    extracted = ExtractResponse(
        nodes=[ExtractedNode(idx=0, name="Serah", type="Person", attributes={})],
        edges=[],
    )
    result = await fetch_node_candidates(
        driver, "vesper", extracted, node_embeddings=[[0.1] * 4], k=5,
    )
    uuids = sorted(c["uuid"] for c in result[0])
    assert uuids == ["u1", "u2"]


@pytest.mark.asyncio
async def test_fetch_node_candidates_skips_empty_embedding():
    driver = MagicMock()
    # name-similar returns []; embedding-similar would error if called with []
    driver.execute_query = AsyncMock(return_value=([], None, None))
    extracted = ExtractResponse(
        nodes=[ExtractedNode(idx=0, name="x", type="Concept", attributes={})],
        edges=[],
    )
    result = await fetch_node_candidates(
        driver, "vesper", extracted, node_embeddings=[[]], k=5,
    )
    assert result == [[]]


# ---------------------------------------------------------------------------
# reconcile_nodes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_nodes_returns_validated_decisions():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={
        "node_decisions": [
            {"extracted_idx": 0, "decision": "existing", "existing_uuid": "u1", "attribute_updates": {}},
            {"extracted_idx": 1, "decision": "new"},
        ],
    })
    extracted = ExtractResponse(nodes=_nodes(2), edges=[])
    candidates = [
        [{"uuid": "u1", "name": "n0", "summary": "...", "attributes": {}, "labels": ["Entity"]}],
        [],
    ]
    result = await reconcile_nodes(llm_client=llm, extracted=extracted, candidates=candidates)
    assert isinstance(result, ReconcileNodesResponse)
    assert result.node_decisions[0].existing_uuid == "u1"
    assert result.node_decisions[1].decision == "new"


@pytest.mark.asyncio
async def test_reconcile_nodes_raises_on_missing_decision():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={
        "node_decisions": [
            {"extracted_idx": 0, "decision": "new"},
            # idx 1 missing
        ],
    })
    extracted = ExtractResponse(nodes=_nodes(2), edges=[])
    with pytest.raises(ValueError, match="missing decision"):
        await reconcile_nodes(llm_client=llm, extracted=extracted, candidates=[[], []])


@pytest.mark.asyncio
async def test_reconcile_nodes_raises_on_extra_decision():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={
        "node_decisions": [
            {"extracted_idx": 0, "decision": "new"},
            {"extracted_idx": 5, "decision": "new"},  # idx 5 doesn't exist
        ],
    })
    extracted = ExtractResponse(nodes=_nodes(1), edges=[])
    with pytest.raises(ValueError, match="unexpected idx"):
        await reconcile_nodes(llm_client=llm, extracted=extracted, candidates=[[]])


# ---------------------------------------------------------------------------
# fetch_edge_candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_edge_candidates_endpoint_path():
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    driver.execute_query.side_effect = [
        ([{
            "uuid": "edge-1", "name": "rel", "fact": "n0 rel n1",
            "source_uuid": "ua", "target_uuid": "ub",
            "valid_at": None, "invalid_at": None,
        }], None, None),
        ([], None, None),  # embedding-similar returns empty
    ]
    extracted = ExtractResponse(nodes=_nodes(2), edges=[_edge()])
    candidates = await fetch_edge_candidates(
        driver, "vesper", extracted,
        node_resolution={0: "ua", 1: "ub"},
        edge_embeddings=[[0.1] * 4], k=5,
    )
    assert candidates[0][0]["uuid"] == "edge-1"


@pytest.mark.asyncio
async def test_fetch_edge_candidates_unions_endpoint_and_embedding():
    driver = MagicMock()
    driver.execute_query = AsyncMock()
    driver.execute_query.side_effect = [
        ([{"uuid": "e1", "name": "r", "fact": "f1",
           "source_uuid": "ua", "target_uuid": "ub",
           "valid_at": None, "invalid_at": None}], None, None),
        ([{"uuid": "e2", "name": "r", "fact": "f2",
           "source_uuid": "ua", "target_uuid": "uc",
           "valid_at": None, "invalid_at": None, "score": 0.8}], None, None),
    ]
    extracted = ExtractResponse(nodes=_nodes(2), edges=[_edge()])
    candidates = await fetch_edge_candidates(
        driver, "vesper", extracted,
        node_resolution={0: "ua", 1: "ub"},
        edge_embeddings=[[0.1] * 4], k=5,
    )
    uuids = sorted(c["uuid"] for c in candidates[0])
    assert uuids == ["e1", "e2"]


# ---------------------------------------------------------------------------
# reconcile_edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_edges_handles_supersession():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={
        "edge_decisions": [
            {"extracted_idx": 0, "decision": "supersedes", "existing_uuid": "old-e"},
        ],
    })
    extracted = ExtractResponse(nodes=_nodes(2), edges=[_edge(fact="now believes")])
    candidates = [[{"uuid": "old-e", "name": "believes", "fact": "used to believe not",
                    "source_uuid": "ua", "target_uuid": "ub",
                    "valid_at": None, "invalid_at": None}]]
    result = await reconcile_edges(llm_client=llm, extracted=extracted, candidates=candidates)
    assert isinstance(result, ReconcileEdgesResponse)
    assert result.edge_decisions[0].decision == "supersedes"
    assert result.edge_decisions[0].existing_uuid == "old-e"


@pytest.mark.asyncio
async def test_reconcile_edges_raises_on_coverage_gap():
    llm = MagicMock()
    llm.generate_response = AsyncMock(return_value={"edge_decisions": []})
    extracted = ExtractResponse(nodes=_nodes(2), edges=[_edge()])
    with pytest.raises(ValueError, match="missing decision"):
        await reconcile_edges(llm_client=llm, extracted=extracted, candidates=[[]])
