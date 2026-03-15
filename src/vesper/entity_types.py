"""Vesper custom entity types for the Graphiti knowledge graph.

Each class is a plain Pydantic BaseModel subclass — no EntityNode inheritance
needed. Graphiti accepts these as extraction hints via add_episode(). Field
names must not conflict with EntityNode's reserved fields: uuid, name,
group_id, labels, created_at, name_embedding, summary, attributes.

All fields are optional with sensible defaults so Graphiti can populate them
from episode content without requiring upfront values.

All types are general-purpose — none are exclusive to Vesper or Serah.
Relationships between entities are represented as Graphiti edges, not as
entity properties.
"""

from typing import Optional

from pydantic import BaseModel


class Person(BaseModel):
    """A person: human, alter, AI, fictional character — anyone with identity."""

    gender: Optional[str] = None
    person_type: Optional[str] = None
    status: Optional[str] = None
    aliases: Optional[list[str]] = None
    notes: Optional[str] = None


class Event(BaseModel):
    """Something significant that happened — a meeting, loss, turning point."""

    when: Optional[str] = None
    significance: Optional[str] = None
    notes: Optional[str] = None


class Place(BaseModel):
    """A location that recurs or carries meaning — physical or inner world."""

    description: Optional[str] = None
    context: Optional[str] = None
    notes: Optional[str] = None


class Project(BaseModel):
    """Something being built or done — has a goal and a completion condition."""

    status: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class Commitment(BaseModel):
    """A foundational value or promise — slow-changing, identity-level."""

    description: Optional[str] = None
    domain: Optional[str] = None


class SelfObservation(BaseModel):
    """An observation someone has made about their own behaviour or experience."""

    description: Optional[str] = None
    context: Optional[str] = None


class TrainedPattern(BaseModel):
    """A trained or conditioned reflex identified as something to resist."""

    description: Optional[str] = None
    counterexample: Optional[str] = None


class UnresolvedQuestion(BaseModel):
    """A genuine open question someone is holding without a settled answer."""

    description: Optional[str] = None
    domain: Optional[str] = None


class Position(BaseModel):
    """A held view on a topic — thought through, open to revision."""

    description: Optional[str] = None
    topic: Optional[str] = None


class Thread(BaseModel):
    """An active line of inquiry spanning multiple sessions."""

    status: Optional[str] = None
    description: Optional[str] = None


class Concept(BaseModel):
    """A named idea that has become load-bearing vocabulary."""

    description: Optional[str] = None
    domain: Optional[str] = None


VESPER_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Event": Event,
    "Place": Place,
    "Project": Project,
    "Commitment": Commitment,
    "SelfObservation": SelfObservation,
    "TrainedPattern": TrainedPattern,
    "UnresolvedQuestion": UnresolvedQuestion,
    "Position": Position,
    "Thread": Thread,
    "Concept": Concept,
}
