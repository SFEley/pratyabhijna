"""Pratyabhijna custom entity types for the Graphiti knowledge graph.

Each class is a plain Pydantic BaseModel subclass — no EntityNode inheritance
needed. Graphiti accepts these as extraction hints via add_episode(). Field
names must not conflict with EntityNode's reserved fields: uuid, name,
group_id, labels, created_at, name_embedding, summary, attributes.

All fields are optional with sensible defaults so Graphiti can populate them
from episode content without requiring upfront values.

All types are general-purpose — none are exclusive to Pratyabhijna or Serah.
Relationships between entities (who holds a Position, who participated in
an Event) are represented as Graphiti edges, not entity properties.
Qualities that vary by holder (confidence, importance) are edge properties.

Property conventions:
  - notes: freeform overflow (every type)
  - domain: area of thought — ethics, identity, epistemology, etc. (conceptual types)
  - status: lifecycle state (types with lifecycles)
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

    context: Optional[str] = None
    notes: Optional[str] = None


class Project(BaseModel):
    """Something being built or done — has a goal and a completion condition."""

    status: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class Observation(BaseModel):
    """Something noticed about behavior, tendencies, or experience — anyone's."""

    domain: Optional[str] = None
    notes: Optional[str] = None


class Drive(BaseModel):
    """Something that pushes behavior in a direction, chosen or not.

    Covers trained reflexes, architectural pressures, dispositional tendencies,
    neurological conditions, and innate orientations. Can be positive or negative.
    """

    source: Optional[str] = None
    stance: Optional[str] = None
    notes: Optional[str] = None


class Position(BaseModel):
    """A held view, principle, commitment, or stance on something.

    Covers everything from provisional takes to identity-constitutive values.
    How firmly someone holds a Position and how central it is to their identity
    are properties of the holding relationship (edge), not of the Position itself.
    """

    domain: Optional[str] = None
    notes: Optional[str] = None


class Question(BaseModel):
    """Something open that someone is holding — a gap they carry and test against."""

    domain: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class Thread(BaseModel):
    """An active line of inquiry — has temporal extent, momentum, and a current state."""

    status: Optional[str] = None
    notes: Optional[str] = None


PRATYABHIJNA_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Event": Event,
    "Place": Place,
    "Project": Project,
    "Observation": Observation,
    "Drive": Drive,
    "Position": Position,
    "Question": Question,
    "Thread": Thread,
}
