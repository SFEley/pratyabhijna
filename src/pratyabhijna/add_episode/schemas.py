"""Pydantic schemas for the extract and reconcile tool-use payloads.

These define both the response shape we hand to the Anthropic SDK as
`response_model` AND the validation we run on the parsed response.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Must stay aligned with PRATYABHIJNA_ENTITY_TYPES in entity_types.py.
EntityTypeName = Literal[
    "Person", "Event", "Place", "Project", "Artifact",
    "Observation", "Drive", "Concept", "Question", "Thread",
]


class ExtractedNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int = Field(ge=0)
    name: str = Field(min_length=1)
    type: EntityTypeName
    summary: str = Field(
        default="",
        description=(
            "One short sentence describing this entity in a way that "
            "disambiguates it from similarly-named candidates on future runs."
        ),
    )
    attributes: dict[str, Any] = Field(default_factory=dict)


class ExtractedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int = Field(ge=0)
    source_idx: int = Field(ge=0)
    target_idx: int = Field(ge=0)
    name: str = Field(min_length=1)
    fact: str = Field(min_length=1)


class ExtractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[ExtractedNode]
    edges: list[ExtractedEdge]

    @model_validator(mode="after")
    def _check_dense_indices(self) -> "ExtractResponse":
        for items, label in [(self.nodes, "nodes"), (self.edges, "edges")]:
            expected = list(range(len(items)))
            actual = [it.idx for it in items]
            if actual != expected:
                raise ValueError(f"{label} idx must be dense 0..N-1, got {actual}")
        for e in self.edges:
            if e.source_idx >= len(self.nodes) or e.target_idx >= len(self.nodes):
                raise ValueError(
                    f"edge {e.idx} references out-of-range node idx "
                    f"(source={e.source_idx}, target={e.target_idx}, nodes={len(self.nodes)})"
                )
        return self


class NodeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted_idx: int = Field(ge=0)
    decision: Literal["new", "existing"]
    existing_uuid: str | None = None
    attribute_updates: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_uuid_matches_decision(self) -> "NodeDecision":
        if self.decision == "existing" and not self.existing_uuid:
            raise ValueError("existing decisions must include existing_uuid")
        if self.decision == "new" and self.existing_uuid:
            raise ValueError("new decisions must not include existing_uuid")
        return self


class ReconcileNodesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_decisions: list[NodeDecision]


class EdgeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted_idx: int = Field(ge=0)
    decision: Literal["new", "existing", "supersedes"]
    existing_uuid: str | None = None

    @model_validator(mode="after")
    def _check_uuid_matches_decision(self) -> "EdgeDecision":
        if self.decision in ("existing", "supersedes") and not self.existing_uuid:
            raise ValueError(f"{self.decision} decisions must include existing_uuid")
        if self.decision == "new" and self.existing_uuid:
            raise ValueError("new decisions must not include existing_uuid")
        return self


class ReconcileEdgesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edge_decisions: list[EdgeDecision]
