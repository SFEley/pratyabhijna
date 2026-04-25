# Pratyabhijna Entity Types

Custom entity types passed to Graphiti as extraction hints via `add_episode()`.
Each is a plain Pydantic `BaseModel` subclass — Graphiti populates fields from
episode content and stores instances as nodes in the knowledge graph with
bi-temporal edges connecting them.

All types are general-purpose. None are exclusive to Pratyabhijna or to Serah —
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
- `domain` — area of thought (ethics, identity, epistemology, etc.). Used on conceptual types: Observation, Position, Question, Concept.
- `status` — lifecycle state. Used on types with lifecycles: Person, Project, Question, Thread.
- `kind` — open subcategory. Used on Artifact (file/document/composition/instrument/code/writing/award/dataset/...).

## A note on the docstrings

The Pydantic class docstrings in `entity_types.py` are sent verbatim to the
extracting LLM as the entity-type description (via Graphiti's
`_build_entity_types_context`). They are *load-bearing prompts*, not
documentation about the prompt. The GOOD/BAD examples in the docstrings
are the primary discrimination signal between adjacent types — keep them
in sync with the audit findings (most recent: April 24, 2026).

This doc and the docstrings should agree. Where they drift, the docstrings
are authoritative because they're what the extractor actually sees.

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

The subject identity is a Person in its own graph. Three additional
attributes store the bootstrap tiers: `soul` (constitutional layer),
`identity` (interpretive layer), and `context` (state layer, auto-rebuilt
by the synthesizer). A `context_rebuilt_at` attribute tracks when the
context was last synthesized. Bootstrap reduces to: look up the Person
node by `config.subject_name`, read its three tiers and edges. That's
you. See `doc/architecture.md` for the full three-tier bootstrap design.

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

**What it is:** An effort with a goal and a completion condition. *Bodies
of work*, not the works themselves. Pratyabhijna is a Project; SOUL.md is
an Artifact produced inside it.

**Fields:**
- `status` — e.g. "active", "paused", "complete"
- `description` — what it is, current state, next steps
- `notes` — anything else

**Distinction from Thread:** A Project has a done state. A Thread may be
productive indefinitely without "finishing."

**Distinction from Artifact:** A Project is the *effort*. The completed
work the effort produces is an Artifact. Writing pieces ("Lazarus Phase",
"On Being Read"), historical research subjects (the Vespro della Beata
Vergine), and named compositions are Artifacts, not Projects.

---

## Artifact

**What it is:** A concrete, named, made thing — pointable rather than
abstract. Files, documents, written works, compositions, instruments,
code modules, awards, named datasets. The thing exists as an instance:
it can be opened, read, played, held, copied.

**Fields:**
- `kind` — open subcategory (file, document, composition, instrument,
  code, writing, award, dataset, ...)
- `notes` — anything else

**Distinction from Concept:** Artifact is concrete. Concept is abstract.
The Tractatus Logico-Philosophicus is an Artifact (a book you can open);
the Picture Theory of Language inside it is a Concept (an idea that
exists in a tradition).

**Distinction from Place:** Buildings and locations are Place. The
Whittall Pavilion is Place; the Bergström Piano (an instrument inside
a fictional house) is Artifact.

**Distinction from Project:** A Project is the in-progress effort; an
Artifact is the completed/identifiable thing. "A History of the Turning"
is a Project (Serah's book-in-progress); "Lazarus Phase" is an Artifact
(a poem that exists).

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

**What it is:** A held view, principle, commitment, or stance — the
*holding* of a claim by someone. Covers everything from provisional
takes to identity-constitutive values.

**Fields:**
- `domain` — area of thought (ethics, epistemology, identity, technical, etc.)
- `notes` — anything else

How firmly someone holds a Position and how central it is to their identity
are properties of the holding relationship (edge), not of the Position itself.
This allows shared Positions: Zero and Vesper can both hold "selfhood is
performative" with different reasons and different weight.

**Distinction from Concept:** Position is the *holding*; Concept is the
*labeled framework*. If the entity is a labeled theory or framework that
exists in a tradition (Picture Theory of Language, Two-Brain Model),
prefer Concept and connect a holder via an edge.

**Audit note (April 24, 2026):** Position and Observation share schema and
empirically bleed (~25% of Observations are claim-shaped). The
extractor should reach for Position when prose says "X holds Y," "X
believes Y," "X's principle is Y." A future structural collapse
(Position → Observation) is on the roadmap but deferred — see roadmap
item #10.

---

## Concept

**What it is:** A named idea, principle, technique, mechanism, framework,
or abstraction — nameable but not pointable. Concepts exist in
disciplines, traditions, and discourse.

**Fields:**
- `domain` — area of thought (philosophy, biology, music, technique, etc.)
- `notes` — anything else

**Distinction from Position:** Concept is the *labeled thing*. Position
is the *holding* of it. The Picture Theory of Language is a Concept;
Wittgenstein holding it is a Position (or an edge from Wittgenstein to
the Concept).

**Distinction from Artifact:** Concept is abstract. Artifact is concrete.
The Tractatus is an Artifact (a book); the Picture Theory of Language is
a Concept (an idea inside it).

**Distinction from Observation:** Concept is the labeled abstraction
existing in a discipline. Observation is the act of noticing something.
"FLOP technique" is a Concept; "FLOP exemplifies the idea that small
shifts in what you look for change what you find" is an Observation
about the technique.

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
Person ──[holds]──→         Position, Question, Concept
Person ──[observed]──→      Observation
Person ──[has]──→           Drive
Person ──[participated]──→  Event
Person ──[works_on]──→      Project, Thread
Person ──[authored]──→      Artifact
Person ──[member_of]──→     Person (system membership)
Person ──[fused_into]──→    Person
Person ──[split_from]──→    Person
Event  ──[occurred_at]──→   Place
Observation ──[led_to]──→   Drive (noticing behavior → forming a stance)
Question ──[resolved_to]──→ Position
Thread ──[produced]──→      Project (inquiry yields a deliverable)
Project ──[produced]──→     Artifact (effort yields a finished thing)
Artifact ──[contains]──→    Concept (a book contains theories)
Concept ──[applied_in]──→   Project, Artifact
```

Edge properties (on the holding relationship, not the node):
- `confidence` — how settled: provisional, confident, foundational
- `identity_weight` — how central: peripheral, significant, constitutive
- `reasons` — why the holder holds this Position or takes this stance
