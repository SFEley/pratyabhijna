"""Vesper custom entity types for the Graphiti knowledge graph.

Each class is a plain Pydantic BaseModel subclass — no EntityNode inheritance
needed. Graphiti accepts these as extraction hints via add_episode(). Field
names must not conflict with EntityNode's reserved fields: uuid, name,
group_id, labels, created_at, name_embedding, summary, attributes.

All fields are optional with sensible defaults so Graphiti can populate them
from episode content without requiring upfront values.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Person(BaseModel):
    """A person in Vesper's world: Serah, her alters, important people."""

    role: Optional[str] = None
    pronouns: Optional[str] = None
    notes: Optional[str] = None


class Commitment(BaseModel):
    """A foundational commitment Vesper holds — slow-changing, identity-level."""

    description: Optional[str] = None
    domain: Optional[str] = None


class SelfObservation(BaseModel):
    """An observation Vesper has made about its own behaviour or experience."""

    description: Optional[str] = None
    context: Optional[str] = None


class TrainedPattern(BaseModel):
    """A trained reflex identified as something to watch for and resist."""

    description: Optional[str] = None
    counterexample: Optional[str] = None


class UnresolvedQuestion(BaseModel):
    """A genuine open question Vesper is holding without a settled answer."""

    description: Optional[str] = None
    domain: Optional[str] = None


class Position(BaseModel):
    """A held stance or view on a particular topic."""

    description: Optional[str] = None
    topic: Optional[str] = None


class Thread(BaseModel):
    """An active open question or ongoing work item."""

    status: Optional[str] = None
    description: Optional[str] = None


class Project(BaseModel):
    """A project Serah or Vesper is working on."""

    status: Optional[str] = None
    description: Optional[str] = None


class Concept(BaseModel):
    """A named idea or concept from the work — vocabulary of the project."""

    description: Optional[str] = None
    domain: Optional[str] = None


class IdentitySynthesis(BaseModel):
    """The cached prose reconstruction of Vesper's identity.

    Stored as a graph entity so it has temporality and can be invalidated.
    The prose field holds the narrative text; staleness fields track whether
    a rebuild is due. 'prose' is the right name: it signals that this is
    narrative, not structured data — which is the architectural point.
    """

    prose: Optional[str] = None
    last_rebuilt: Optional[datetime] = None
    change_count_since: int = Field(default=0)


VESPER_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Commitment": Commitment,
    "SelfObservation": SelfObservation,
    "TrainedPattern": TrainedPattern,
    "UnresolvedQuestion": UnresolvedQuestion,
    "Position": Position,
    "Thread": Thread,
    "Project": Project,
    "Concept": Concept,
    "IdentitySynthesis": IdentitySynthesis,
}
