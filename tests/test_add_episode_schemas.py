"""Pydantic schemas for the extract and reconcile tool-use payloads.

Validation lives in the schema models themselves (model_validator hooks);
these tests exercise both the happy path and the validation rules.
"""

import pytest

from pratyabhijna.add_episode.schemas import (
    EdgeDecision,
    ExtractedEdge,
    ExtractedNode,
    ExtractResponse,
    NodeDecision,
    ReconcileEdgesResponse,
    ReconcileNodesResponse,
)


# --- ExtractedNode / ExtractedEdge ----------------------------------------


def test_extracted_node_minimal():
    n = ExtractedNode(idx=0, name="Serah", type="Person", attributes={})
    assert n.idx == 0
    assert n.type == "Person"


def test_extracted_node_rejects_unknown_type():
    with pytest.raises(ValueError):
        ExtractedNode(idx=0, name="X", type="Bogus", attributes={})


def test_extracted_node_rejects_empty_name():
    with pytest.raises(ValueError):
        ExtractedNode(idx=0, name="", type="Person", attributes={})


def test_extracted_edge_minimal():
    e = ExtractedEdge(
        idx=0, source_idx=0, target_idx=1,
        name="works_on", fact="Serah works on Pratyabhijna.",
    )
    assert e.source_idx == 0


# --- ExtractResponse ------------------------------------------------------


def test_extract_response_dense_indices():
    resp = ExtractResponse(
        nodes=[
            ExtractedNode(idx=0, name="A", type="Person", attributes={}),
            ExtractedNode(idx=1, name="B", type="Concept", attributes={}),
        ],
        edges=[
            ExtractedEdge(idx=0, source_idx=0, target_idx=1, name="rel", fact="A rel B"),
        ],
    )
    assert len(resp.nodes) == 2


def test_extract_response_rejects_sparse_node_indices():
    with pytest.raises(ValueError, match="dense"):
        ExtractResponse(
            nodes=[
                ExtractedNode(idx=0, name="A", type="Person", attributes={}),
                ExtractedNode(idx=2, name="C", type="Concept", attributes={}),  # gap
            ],
            edges=[],
        )


def test_extract_response_rejects_edge_pointing_to_missing_node():
    with pytest.raises(ValueError, match="out-of-range"):
        ExtractResponse(
            nodes=[ExtractedNode(idx=0, name="A", type="Person", attributes={})],
            edges=[
                ExtractedEdge(idx=0, source_idx=0, target_idx=5, name="rel", fact="x"),
            ],
        )


# --- NodeDecision ---------------------------------------------------------


def test_node_decision_existing_requires_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        NodeDecision(extracted_idx=0, decision="existing")


def test_node_decision_new_forbids_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        NodeDecision(extracted_idx=0, decision="new", existing_uuid="abc")


def test_node_decision_new_minimal():
    d = NodeDecision(extracted_idx=0, decision="new")
    assert d.existing_uuid is None
    assert d.attribute_updates == {}


def test_node_decision_existing_with_attribute_updates():
    d = NodeDecision(
        extracted_idx=0,
        decision="existing",
        existing_uuid="u1",
        attribute_updates={"status": "active"},
    )
    assert d.attribute_updates == {"status": "active"}


# --- EdgeDecision ---------------------------------------------------------


def test_edge_decision_supersedes_requires_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        EdgeDecision(extracted_idx=0, decision="supersedes")


def test_edge_decision_existing_requires_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        EdgeDecision(extracted_idx=0, decision="existing")


def test_edge_decision_new_forbids_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        EdgeDecision(extracted_idx=0, decision="new", existing_uuid="x")


def test_edge_decision_new_minimal():
    d = EdgeDecision(extracted_idx=0, decision="new")
    assert d.existing_uuid is None


# --- Reconcile wrappers ---------------------------------------------------


def test_reconcile_nodes_response_validates_inner_decisions():
    with pytest.raises(ValueError):
        ReconcileNodesResponse(node_decisions=[
            NodeDecision(extracted_idx=0, decision="existing"),  # missing uuid
        ])


def test_reconcile_edges_response_validates_inner_decisions():
    with pytest.raises(ValueError):
        ReconcileEdgesResponse(edge_decisions=[
            EdgeDecision(extracted_idx=0, decision="supersedes"),
        ])
