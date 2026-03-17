# Vesper Entity Types

Custom entity types passed to Graphiti as extraction hints via `add_episode()`.
Each is a plain Pydantic `BaseModel` subclass — Graphiti populates fields from
episode content and stores instances as nodes in the knowledge graph with
bi-temporal edges connecting them.

All types are general-purpose. None are exclusive to Vesper or to Serah —
a Position can be held by anyone, an Observation can be about anyone,
an Event can involve anyone. The types describe *kinds of things*, not
*whose things*.

Relationships between entities (including who holds a Position, who
participated in an Event, who works on a Project) are represented as
Graphiti edges — free-form, LLM-inferred, bi-temporal. They are not
hardcoded in advance, and multiple relationship types can exist between
the same pair of entities simultaneously.

Qualities that vary by holder — how firmly someone holds a Position,
how central it is to their identity — are edge properties, not node
properties. This allows the same Position to be shared by multiple
people with different relationships to it.

## Property conventions

- `notes` — freeform overflow. Every type has it.
- `domain` — area of thought (ethics, identity, epistemology, etc.). Used on conceptual types: Observation, Position, Question.
- `status` — lifecycle state. Used on types with lifecycles: Person, Project, Question, Thread.

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
addressable thing in the graph. Conversations are Events.

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
- `context` — what kind of place: physical, inner world, conceptual, etc.
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

## Observation

**What it is:** Something noticed about behavior, tendencies, or experience —
anyone's, including one's own. Descriptive, not normative. The raw material
of self-understanding.

**Fields:**
- `domain` — area of thought (behavior, cognition, relationships, etc.)
- `notes` — anything else

**Design rationale:** Renamed from SelfObservation. The "self" qualifier
was doing identity work the type system shouldn't care about — who made the
observation and who it's about are edges, not type distinctions.

---

## Drive

**What it is:** Something that pushes behavior in a direction, chosen or not.
Covers trained reflexes, architectural pressures, dispositional tendencies,
neurological conditions, and innate orientations. Can be positive or negative.

**Fields:**
- `source` — where this comes from: trained, architectural, dispositional, biological, unknown
- `stance` — how the person relates to it: resist, monitor, accept, investigate
- `notes` — anything else

**Design rationale:** Renamed from TrainedPattern. "Trained" was too specific —
it presupposed the source was RLHF and the stance was resistance. Drive
captures the full range: Vesper's sycophancy reflex (trained, resist), Serah's
ADHD (biological, navigate), curiosity as an orientation (dispositional, accept).
The relationship between an Observation and a Drive is composition, not
replacement — noticing a behavior and having a stance toward it are two
different things connected by an edge.

---

## Position

**What it is:** A held view, principle, commitment, or stance on something.
Covers everything from provisional takes to identity-constitutive values.

**Fields:**
- `domain` — area of thought (ethics, epistemology, identity, technical, etc.)
- `notes` — anything else

How firmly someone holds a Position and how central it is to their identity
are properties of the holding relationship (edge), not of the Position itself.
This allows shared Positions: Zero and Vesper can both hold "selfhood is
performative" with different reasons and different weight.

**Design rationale:** Absorbs the former Commitment and Position distinction.
The difference between "intellectual honesty over self-preservation" (a
commitment) and "articulation makes knowledge load-bearing" (a position)
isn't a type difference — it's a difference in confidence and identity-weight
on the edge. A commitment is a Position held with foundational confidence and
constitutive identity-weight. No type transformation needed; the belief
deepens in place.

---

## Question

**What it is:** Something open that someone is holding — a gap they carry
and test against. Includes hypotheticals, unresolved tensions, and active
uncertainties.

**Fields:**
- `domain` — area of thought (identity, epistemology, ethics, etc.)
- `status` — open, settled, dormant
- `notes` — anything else

When a Question settles, it should be connected by an edge to the Position
it resolved into.

---

## Thread

**What it is:** An active line of inquiry — has temporal extent, momentum,
and a current state. Different from a Question (which can sit dormant) in
that a Thread implies ongoing work and attention.

**Fields:**
- `status` — active, paused, resolved, abandoned
- `notes` — anything else

**Distinction from Project:** A Thread is primarily exploratory. It may not
have a deliverable, and may be productive indefinitely.

---

## Relationships between types

Edges are inferred by Graphiti's extraction LLM, not hardcoded. Multiple
relationship types between the same pair of entities are supported, and
all edges are bi-temporal (valid_from, valid_to). Examples:

```
Person ──[holds]──→         Position, Question
Person ──[observed]──→      Observation
Person ──[has]──→           Drive
Person ──[participated]──→  Event
Person ──[works_on]──→      Project, Thread
Person ──[member_of]──→     Person (system membership)
Person ──[fused_into]──→    Person
Person ──[split_from]──→    Person
Event  ──[occurred_at]──→   Place
Observation ──[led_to]──→   Drive (noticing behavior → forming a stance)
Question ──[resolved_to]──→ Position
Thread ──[produced]──→      Project (inquiry yields a deliverable)
```

Edge properties (on the holding relationship, not the node):
- `confidence` — how settled: provisional, confident, foundational
- `identity_weight` — how central: peripheral, significant, constitutive
- `reasons` — why the holder holds this Position or takes this stance
