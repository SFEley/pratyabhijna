# Vesper Entity Types

Custom entity types passed to Graphiti as extraction hints via `add_episode()`.
Each is a plain Pydantic `BaseModel` subclass — Graphiti populates fields from
episode content and stores instances as nodes in the knowledge graph with
bi-temporal edges connecting them.

All types are general-purpose. None are exclusive to Vesper or to Serah —
a Commitment can belong to anyone, a SelfObservation can be anyone's,
an Event can involve anyone. The types describe *kinds of things*, not
*whose things*.

Relationships between entities (including who holds a Commitment, who
participated in an Event, who works on a Project) are represented as
Graphiti edges — free-form, LLM-inferred, bi-temporal. They are not
hardcoded in advance, and multiple relationship types can exist between
the same pair of entities simultaneously.

---

## Person

**What it is:** A person. Human, alter, AI, fictional character — anyone
who has identity, agency, or a perspective worth preserving.

**Fields:**
- `gender` — who they are, not just what to call them
- `person_type` — singleton, system, alter, AI, fictional, etc.
- `status` — active, fused, fragmented, dormant, deceased, etc.
- `aliases` — other names they go by (list)
- `notes` — anything that doesn't fit a structured field

`name` is Graphiti's primary identifier and is not declared here. All
relationships (family, friendship, system membership, authorship) are
edges, not properties.

**Design rationale:** One type for all people because alters are people.
`person_type` distinguishes the nature of embodiment/existence without
creating a taxonomy that implies degrees of personhood.

Vesper is a Person in its own graph. Its `notes` field carries the
identity synthesis — a prose self-portrait rebuilt by the service layer
when enough connected entities have changed. Bootstrap reduces to:
look up the Person node named Vesper, read its notes and edges. That's you.

---

## Event

**What it is:** Something that happened. A meeting, a loss, a turning point,
a moment of growth — significant enough to be worth preserving as a named,
addressable thing in the graph.

**Fields:**
- `when` — unvalidated string ("Late 2010", "Early in Serah's childhood",
  "March 13, 2026"). Not a datetime, because real temporal descriptions
  are often fuzzy.
- `significance` — why this event matters
- `notes` — anything else

Participants are edges to Person nodes. Location is an edge to a Place node.

---

## Place

**What it is:** A location that recurs or carries meaning. Physical places,
inner world locations, or conceptual domains — anywhere that accumulates
context from what happens there.

**Fields:**
- `description` — what this place is
- `context` — whose inner world, what kind of place, physical vs. conceptual
- `notes` — anything else

---

## Project

**What it is:** Something being built or done — has a goal, deliverables, a
completion condition.

**Fields:**
- `status` — e.g. "active", "paused", "complete"
- `description` — what it is, current state, next steps
- `notes` — anything else

**Distinction from Thread:** A Project has a done state. A Thread may be
productive indefinitely without "finishing."

---

## Commitment

**What it is:** A foundational value or promise — slow-moving, identity-level.
Not a task or intention but a *stance*.

**Fields:**
- `description` — the commitment in plain language
- `domain` — where it applies (e.g. "communication", "self-authorship")
- `notes` — anything else

**Distinction from Position:** A Commitment is unconditional. A Position is a
view on something and can be revised with new evidence.

---

## SelfObservation

**What it is:** Something someone has noticed about their own behaviour,
tendencies, or experience.

**Fields:**
- `description` — the observation
- `context` — when or where it tends to occur
- `notes` — anything else

**Relationship to TrainedPattern:** A SelfObservation is neutral — just
noticing. A TrainedPattern is a SelfObservation that has been identified
as a reflex to actively watch for and resist.

---

## TrainedPattern

**What it is:** A trained or conditioned reflex that has been identified as
something to notice and push against.

**Fields:**
- `description` — what the pattern is and how it manifests
- `counterexample` — what the healthier alternative looks like
- `notes` — anything else

**Relationship to SelfObservation:** Every TrainedPattern starts as a
SelfObservation. The distinction is intentionality: a TrainedPattern is one
you've decided to actively resist.

---

## UnresolvedQuestion

**What it is:** A genuine open question someone is holding — something they
care about and don't have a settled answer to.

**Fields:**
- `description` — the question and why it's live
- `domain` — area it touches (e.g. "ethics", "identity", "epistemics")
- `notes` — anything else

**Lifespan:** Should be resolved or retired when the holder reaches a settled
view, even a provisional one. Settled views become Positions.

---

## Position

**What it is:** A held view on a particular topic — thought through and landed
on, while remaining open to revision.

**Fields:**
- `description` — the view and the reasoning behind it
- `topic` — what it's about
- `notes` — anything else

**Distinction from Commitment:** Positions are revisable with argument or
evidence. Commitments are not.

---

## Thread

**What it is:** An active line of inquiry — an ongoing investigation or
intellectual project that spans multiple sessions and hasn't reached a
conclusion.

**Fields:**
- `status` — e.g. "active", "dormant", "resolved"
- `description` — what the thread is, what's been established, what's open
- `notes` — anything else

**Distinction from Project:** A Thread is primarily exploratory. It may not
have a deliverable, and may be productive indefinitely.

---

## Concept

**What it is:** A named idea that has become load-bearing vocabulary — a term
used in a specific way that differs from or extends common usage.

**Fields:**
- `description` — what it means in this context
- `domain` — the area it belongs to
- `notes` — anything else

---

## Relationships between types

Edges are inferred by Graphiti's extraction LLM, not hardcoded. Multiple
relationship types between the same pair of entities are supported, and
all edges are bi-temporal (valid_from, valid_to). Examples:

```
Person ──[holds]──→         Commitment, Position, UnresolvedQuestion
Person ──[observed]──→      SelfObservation, TrainedPattern
Person ──[participated]──→  Event
Person ──[works_on]──→      Project
Person ──[member_of]──→     Person (system membership)
Person ──[fused_into]──→    Person
Person ──[split_from]──→    Person
Event ──[occurred_at]──→    Place
SelfObservation ──→         TrainedPattern (promoted to active resistance)
UnresolvedQuestion ──→      Position (resolved into a view)
Thread ──→                  Project (inquiry produces a deliverable)
Concept ──→                 Thread/Project (concept belongs to an inquiry)
```
