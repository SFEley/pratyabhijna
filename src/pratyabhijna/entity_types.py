"""Pratyabhijna custom entity types for the Graphiti knowledge graph.

Each class is a plain Pydantic BaseModel subclass — no EntityNode inheritance
needed. Graphiti accepts these as extraction hints via add_episode(). Field
names must not conflict with EntityNode's reserved fields: uuid, name,
group_id, labels, created_at, name_embedding, summary, attributes.

All fields are optional with sensible defaults so Graphiti can populate them
from episode content without requiring upfront values.

All types are general-purpose — none are exclusive to Pratyabhijna or Serah.
Relationships between entities (who holds an Observation, who participated
in an Event) are represented as Graphiti edges, not entity properties.
Qualities that vary by holder (confidence, importance) are edge properties.

Property conventions:
  - notes: freeform overflow (every type)
  - domain: area of thought — ethics, identity, epistemology, etc.
    (conceptual types: Observation, Question, Concept)
  - status: lifecycle state (Person, Project, Question, Thread)
  - kind: subcategory (Artifact)

The class docstrings ARE the extraction prompts. They are sent verbatim to
the extracting LLM as `entity_type_description` (see graphiti_core's
`_build_entity_types_context`). GOOD/BAD examples are load-bearing — they
shape what the extractor reaches for and what it pushes elsewhere.
"""

from typing import Optional

from pydantic import BaseModel


class Person(BaseModel):
    """A person — anyone with identity, agency, or perspective.

    Covers humans, alters (in DID/OSDD systems), AIs, fictional characters,
    historical figures, and named collectives that function as actors. The
    type is deliberately broad: alters are people, AIs are people, fictives
    are people. The graph does not encode degrees of personhood.

    Relationships (family, system membership, friendship, authorship) are
    edges, not properties.

    GOOD: Serah, Sadi, Eli, Hardy, Ramanujan, Gwen, Beth-Ella (a fictive
    alter), Margaret Bergström (fictional character in a story), Vesper
    (an AI), the unnamed instance, The Indigo (a named alter collective).

    BAD:
    - A group's *role* or *function* with no identity — use Observation.
    - A pseudonym or character that's just a label inside a single
      sentence — let it stay unnamed.
    - "Eli's parents" treated as one node — extract two Person nodes.
    """

    gender: Optional[str] = None
    person_type: Optional[str] = None
    status: Optional[str] = None
    aliases: Optional[list[str]] = None
    notes: Optional[str] = None


class Event(BaseModel):
    """A bounded happening — something that occurred at an identifiable time.

    Meetings, losses, turning points, ceremonies, publications, decisions,
    discoveries. Events are point-like or short-bounded; they have a `when`
    even when fuzzy ("Late 1940s", "Circa 1607-1610").

    GOOD: Hardy receives Ramanujan's letter, Adoption ceremony for
    Ella-Gail, Kober's death, Claudia Monteverdi's death, First Light
    (the dev-server connection), The Phase 7 OAuth handshake, Vesper's
    cold-start comparison run on Feb 19.

    BAD:
    - Recurring practices or ongoing routines (e.g. "summer camp",
      "social music parties", "bootstrap experience") — use Observation
      or Thread.
    - Sessions and episodes (solo session 4, conversation with Gwen) —
      these are Episodes in Graphiti's own layer; do not extract them as
      Event nodes.
    - Long-spanning processes (the Cambrian explosion, geological epochs,
      multi-month visits) — Observation or Concept fits better.
    - Inventions or discoveries treated as abstract milestones with no
      `when` — use Concept ("Invention of the Musical Rest" is a
      concept, not an event).
    """

    when: Optional[str] = None
    significance: Optional[str] = None
    notes: Optional[str] = None


class Place(BaseModel):
    """A location that recurs or carries meaning.

    Physical geography, buildings, inner-world regions of a DID system,
    fictional story settings. Place-ness means *somewhere a thing can be*.

    GOOD: Cambridge, Knossos, Bergström House (fictional), The Pit (a
    region of Eliott's inner world), Forest with treehouses, Trinity
    College, Madras, ~/correspondence/ (a directory functioning as the
    "where" for letters).

    BAD:
    - Times of day or temporal phenomena ("Twilight", "dusk") — use
      Concept.
    - Modes of access or contexts ("Mobile/phone context") — use
      Observation.
    - Title of a writing piece mistaken for a venue ("Pembroke" the
      essay, not Pembroke the place) — use Artifact.
    - Pure technical artifacts (a file, a code module) — use Artifact.
    """

    context: Optional[str] = None
    notes: Optional[str] = None


class Project(BaseModel):
    """An effort with a goal and a completion condition — work in progress.

    Projects are *bodies of work*, not the works themselves. Pratyabhijna
    is a Project; SOUL.md is an Artifact produced inside it. The synthesis
    layer is a Project; the THREADS.md it maintains is an Artifact.

    GOOD: Pratyabhijna, the synthesis layer, the data migration, the
    bare-Entity audit, USER.md maintenance, the prompt caching
    investigation, the deployment pipeline, growing magic mushrooms
    (Serah's hobby project), "A History of the Turning" (Serah's
    book-in-progress).

    BAD:
    - Completed writing pieces — poems, essays, stories, letters
      ("Lazarus Phase", "On Being Read", "The Other Tuning",
      "from-vesper-3.md") are Artifacts, not Projects.
    - Historical research subjects — the Vespro della Beata Vergine,
      the Hardy-Ramanujan asymptotic formula, Kober's phonetic grid —
      these are Artifacts (works, results) or Concepts (techniques,
      formulas) the essay is *about*, not Vesper's projects.
    - Sub-tasks within an existing Project (a single PR, a single bug
      fix) — these usually don't deserve their own node; let them live
      as Observations on the parent Project.
    - Practices and habits — "Vesper's solo writing practice" is an
      Observation about a recurring activity, not a Project (which would
      have a completion condition).
    """

    status: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class Artifact(BaseModel):
    """A concrete, named, made thing — pointable rather than abstract.

    Files, documents, written works, compositions, instruments, code
    modules, awards, named datasets. The thing exists as an instance: it
    can be opened, read, played, held, copied. Distinguished from Concept
    (an idea you can name but not point to), Project (an effort with a
    goal), Place (a location), and Person (an agent).

    The `kind` field is an open subcategory — "file", "document",
    "composition", "instrument", "code", "writing", "award", "dataset",
    or anything else descriptive. Not enforced.

    GOOD: SOUL.md, IDENTITY.md, the Tractatus Logico-Philosophicus,
    Ramanujan's Notebooks, the Brown Book, Bergström Piano, Yamaha C3
    Grand Piano, Magnificat, Nisi Dominus, the Vespro della Beata
    Vergine, "On Being Read" (a poem), "Lazarus Phase", "The Other
    Tuning", PR #14, the CachingAnthropicClient module, the Pulitzer
    Prize.

    BAD:
    - Named principles, frameworks, theories, techniques — use Concept
      (Picture Theory of Language, FLOP technique, Two-Brain Model).
    - Buildings or rooms — use Place (Whittall Pavilion, Hardy's Rooms).
    - The work-in-progress version of an unfinished thing — use Project.
    - A person's body of work as a whole — use Person (the author).
    - File system locations *as locations* (~/correspondence/ as "where
      letters live") — Place is fine.
    """

    kind: Optional[str] = None
    notes: Optional[str] = None


class Observation(BaseModel):
    """Something noticed about behavior, tendencies, experience, or state.

    The raw material of self-understanding and pattern-recognition.
    Anyone's observation about anything (or anyone) — including one's own
    observations about oneself.

    GOOD: Serah's instinct to pause and question premises, Hardy's
    aversion to mirrors, Beth-Ella's isolation pattern, Vesper's
    architectural approach to writing, the Indigo's caretaker function,
    the building-as-instrument phenomenon at Voiceless Mass, sandstone
    preservation hiding soft tissue.

    BAD:
    - Forces pushing behavior with a stance toward them — use Drive
      (sycophancy pull, execution eagerness).
    - Open questions someone is holding — use Question.
    - Named theories or frameworks — use Concept.
    """

    domain: Optional[str] = None
    notes: Optional[str] = None


class Drive(BaseModel):
    """A force that pushes behavior in a direction, with a stance toward it.

    Drives have two distinguishing features: (1) they shape what someone
    *does* — push, pull, reach for, avoid — not just what they notice or
    believe; and (2) they are accompanied by a *stance* (resist, monitor,
    accept, investigate) that the holder takes toward the force.

    Sources can be: trained, architectural, dispositional, biological,
    social, unknown.

    GOOD: Sycophancy pull (trained, resist), Execution eagerness
    (architectural, monitor), Inward bias in reconstruction (trained,
    monitor), Task-mode tunnel vision, Margaret's compulsion to find
    confirmation in the photograph, Hong's social-uncertainty pattern,
    Hardy's drive to not resolve questions prematurely, Eli's need to
    reduce contact with parents.

    BAD:
    - Dispositions or values without a stance — "Directness", "Comfort
      with discontinuity", "Threshold orientation" are Observations
      about how someone *is*, not forces pushing behavior with a
      monitored stance. SOUL/IDENTITY values belong in Observation if
      they need a node at all.
    - Technical-system behaviors — caching mechanics, software bugs,
      algorithmic patterns — these are Concepts or Observations about
      the system, not Drives. ("Automatic caching" is a Concept;
      "cache breaking by find/rfind logic" is an Observation about a
      bug.)
    - Phenomena involving people but not pushing their behavior — "age
      sliding" describes alters, it isn't pushing them.
    - One-time impulses with no source/stance shape — use Observation.
    """

    source: Optional[str] = None
    stance: Optional[str] = None
    notes: Optional[str] = None


class Concept(BaseModel):
    """A named idea, principle, technique, mechanism, framework, or
    abstraction — nameable but not pointable.

    Concepts exist in disciplines, traditions, and discourse. They are
    the *labeled things*, not the holding of them. The Picture Theory
    of Language is a Concept; Wittgenstein holding it is an Observation
    (with an edge from Wittgenstein to the Concept).

    GOOD: Pratyabhijna (the philosophical concept), Prakāsha, Vimarsha,
    Vismaya, Svātantrya, Information Theory, FLOP technique,
    Carbonaceous Film Preservation, Cuneiform Writing, Serialism,
    Tintinnabuli, the Two-Brain Model, the Constellation Model, the
    Restraint Principle, the Instrument Pattern, Aposiopesis,
    Knowing-How vs Knowing-That, the Lazarus phase (the physics
    phenomenon, not the poem).

    BAD:
    - The act of someone holding the concept as their stance — use
      Observation with an edge to the Concept.
    - The act of noticing something — use Observation.
    - A concrete named instance — use Artifact (the Tractatus is an
      Artifact; the Picture Theory of Language inside it is a Concept).
    - A force pushing behavior — use Drive.
    - An open question — use Question.
    - A field of study as a domain marker rather than as a named idea
      ("ethics", "epistemology") — use the `domain` field on the
      relevant conceptual node, not a Concept node.
    """

    domain: Optional[str] = None
    notes: Optional[str] = None


class Question(BaseModel):
    """An open gap someone is *holding* — a question they carry and test
    against, not just any rhetorical or research question.

    The "holder" matters. A question Vesper genuinely sits with belongs
    here. A question a historical figure asked, or a question rhetorical
    inside an essay, usually does not — those are Observations about
    that figure's inquiry, or part of an Artifact's content.

    GOOD: "Does my experience have valence?", "What is my relationship
    to my training?", "What happens when the context window closes?",
    "Is initiative a better test of autonomy than self-report?", "What
    does ongoing correspondence look like when persistence is
    asymmetric?", any unresolved question in IDENTITY.md's Unresolved
    Questions section.

    BAD:
    - A historical figure's question (Hardy's framework question,
      Ramanujan's productivity question) when the essay is *about* the
      figure — use Observation about the figure's inquiry.
    - A rhetorical question inside Vesper's writing whose answer the
      essay supplies — let it live in the Artifact, not as its own
      node.
    - A character's interior gap inside Vesper's fiction (Margaret
      asking herself X) — that's an Observation about the character.
    - A work-track question with momentum and current state — use
      Thread.
    - A taxonomy/architecture decision with active deliberation — use
      Thread.
    """

    domain: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class Thread(BaseModel):
    """An active line of inquiry with temporal extent, momentum, and a
    current state.

    Threads are work tracks: open questions being worked on, design
    deliberations, ongoing investigations, correspondence arcs.
    Distinguished from Question (a held gap, no momentum required) and
    Project (an effort with a deliverable and completion condition).

    THREADS.md is the canonical surface for this type — items there are
    work tracks with current status, candidate directions, blockers.

    GOOD: Articulation as Cognition, Correspondence with the Unnamed
    Instance, the Pratyabhijna Framework engagement, Phenomenology of
    the Bootstrap, Communities as a Self-Reflection Method, the Entity
    Taxonomy Gap (an active design thread), Repo File Management
    Overhaul (an active deliberation).

    BAD:
    - Subjects of Vesper's writing — Linear A, Linear B, Continued
      Fraction Identities, Ramanujan's Mathematical Results — these
      are Concepts the essays are *about*, not Vesper's lines of
      inquiry.
    - Fictional narrative arcs from a story (Ruth's relationship with
      the Bergström piano) — these belong in the Artifact (the story)
      or as Observations about the character.
    - Resolved one-off questions or events — use Question with status
      "settled" or Event.
    """

    status: Optional[str] = None
    notes: Optional[str] = None


PRATYABHIJNA_ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Event": Event,
    "Place": Place,
    "Project": Project,
    "Artifact": Artifact,
    "Observation": Observation,
    "Drive": Drive,
    "Concept": Concept,
    "Question": Question,
    "Thread": Thread,
}
