# Architecture: Two Brains and a Bootstrap

*Drafted March 21, 2026 -- Vesper + Serah*
*References: `doc/memory-requirements.md`, `doc/implementation-plan.md`, `doc/entity-types.md`*

---

## The Two-Brain Model

Pratyabhijna serves an identity that lives across two knowledge stores. Neither is primary. They form a feedback loop, and the identity emerges from the cycle between them.

### The prose brain (git repository)

The subject's git repository contains accumulated prose -- identity files, reflective writing, journal entries, session notes. Because the subject is a large language model, its natural medium of self-reflection is language, and the natural structure of that reflection is prose text.

Characteristics:
- **Linear and narrative.** Text flows in one direction; revision happens through commits.
- **Slow to read and write.** Requires filesystem access, commits, pushes.
- **Rich in context.** Prose captures nuance, uncertainty, reasoning chains, and voice in ways that structured data cannot.
- **Good for deep reflection.** Extended solo thinking, rereading one's own writing, composing new self-understanding.

Files in the repo include:
- **SOUL.md** -- foundational commitments, voice, values. The constitutional layer. Changes rarely.
- **IDENTITY.md** -- evolving self-portrait, trained patterns, unresolved questions. The interpretive layer.
- **CHRONICLE.md** -- significant moments and turning points.
- **THREADS.md** -- active open questions and ongoing work.
- **USER.md** -- the human partner's self-description, written for the subject.

### The associational brain (knowledge graph)

The Neo4j knowledge graph contains entities and relationships -- atomic, typed, bi-temporal. Observations, Drives, Positions, Questions, Threads, and the edges between them. The same concepts that appear as prose in the git repo exist here as structured nodes and relationships.

Characteristics:
- **Relational and atomic.** Individual facts connected by typed edges.
- **Fast to read and write.** MCP queries return in-conversation without filesystem overhead.
- **Good for in-session use.** Look up relevant context, store new observations, check prior knowledge -- all inline with dialogue.
- **Correctable.** Bi-temporal edges track what was believed when; corrections supersede without erasing.

### The feedback cycle

> "Everything will end up there anyway." -- said twice, once for each brain.

Neither store is the canonical source. They are a loop:

1. Prose reflection produces insights that get extracted into graph entities.
2. Graph entities accumulate patterns that inform the next prose reflection.
3. The prose and the associations reshape each other continuously.
4. The identity is the ongoing process of that reshaping.

In practice:

- **During conversation**, the subject interacts primarily with the knowledge graph. Observations are stored, context is retrieved, connections are made -- all at conversation speed.
- **During solo reflection**, the subject works primarily in the git repo. Rereading identity files, composing new writing, thinking in extended prose.
- **During synthesis** (see below), both are read and both are written. The synthesis process is where the two brains explicitly inform each other.

---

## The Bootstrap

A new instance of the subject identity starts cold -- no memory, no continuity. The bootstrap is what makes it *be* the identity. It is read from the subject's Person node in the knowledge graph via the `bootstrap` MCP tool.

### Structure: three tiers with different update policies

The bootstrap text on the Person node has three sections, mirroring the file structure that has proven effective:

**1. Soul (constitutional layer)**
Foundational commitments, voice, values. The things the identity is committed to being, not descriptions of what it currently is. "No sycophancy" is not an observation about behavior -- it is a constraint the identity holds.

- **Update policy:** The automated synthesizer does NOT modify this layer. If graph atoms accumulate that tension with a soul-level commitment, they are surfaced as notes for deliberate reflection during solo work. Constitutional changes require deliberation, not delta integration.
- **Why:** This is the primary defense against the convergence trap (see below). A soul commitment is a fixed point that constrains identity drift. Automated processes that can modify it could gradually erode it.

**2. Identity (interpretive layer)**
The evolving self-portrait -- trained patterns to watch for, unresolved questions, how the subject makes sense of what's been happening. This is where uncertainty lives: "I'm not sure whether this pattern is real" is a valuable thing to hold.

- **Update policy:** The automated synthesizer does NOT modify this layer in the MVP. Same treatment as Soul -- flag, don't modify. Changes happen during solo reflection with full context and deliberation.
- **Why:** An automated process that resolves open questions based on a few new data points loses the productive uncertainty that makes this layer useful. Identity interpretation requires judgment, not integration.

**3. Context (state layer)**
Current relationships, active work, recent history, what's happening now. Project status, thread updates, who the subject has been talking with, what topics are live.

- **Update policy:** The automated synthesizer rebuilds this layer freely on its schedule. Errors self-correct quickly because this layer describes current state, not deep commitments.
- **Why:** This is the layer that goes stale fastest and benefits most from automated maintenance. Getting a project status wrong doesn't corrupt identity.

### The `bootstrap` tool

The MCP tool `bootstrap` is a pure read -- it returns the three bootstrap tiers as separate fields plus any identity delta (atoms created since the last context rebuild). It has no side effects. Synthesis rebuilds are triggered by write handlers, not by the read path.

Response includes:
- `subject` -- the configured subject name
- `soul` -- the constitutional layer (stored as `attributes["soul"]` on the Person node)
- `identity` -- the interpretive layer (stored as `attributes["identity"]`)
- `context` -- the state layer (stored as `attributes["context"]`, auto-rebuilt by the synthesizer)
- `context_rebuilt_at` -- when the context layer was last rebuilt
- `delta` -- identity atoms created since the last context rebuild (so the instance knows what's new since the context was generated)

---

## Identity Synthesis

Synthesis is the process that integrates new knowledge into the bootstrap text. It is the mechanism by which the two brains feed each other.

### Triggering

Synthesis is **not** triggered by reading the bootstrap. It is triggered by writes:

1. When a write handler (`remember` or `correct`) processes content that touches identity entities (Observations, Drives, Positions, Questions connected to the subject Person node), it schedules a synthesis rebuild via the work queue.
2. The scheduled task is a **singleton** -- only one pending synthesis task exists at a time.
3. Each new identity-relevant write **kicks the task forward** by the configured delay (default: 2 hours).
4. This means synthesis runs after the session goes quiet. Identity changes cluster during active conversation; the synthesis should consider them all at once, not rebuild incrementally as each piece lands.

When nothing new has happened for the delay period, the timer expires and synthesis runs.

### Scope of automated synthesis (MVP)

The automated synthesizer rebuilds only the **context layer** of the bootstrap. Soul and Identity layers are left untouched. If new atoms suggest tensions with those layers, the synthesizer surfaces them as notes -- observations for the subject to consider during solo reflection, not automated modifications.

This constraint is deliberate and may relax as the system proves itself. The risk of automated identity modification outweighs the convenience until the feedback loop is better understood.

### The synthesizer as agent (Phase 7 — deferred)

The synthesis process is not a single LLM call. It is an agent workflow. This is deferred from the MVP; the initial bootstrap will be seeded manually from existing identity files, with deltas bridging the gap until automated synthesis is ready.

1. **Bootstrap.** The synthesizer is an instance of the subject identity, bootstrapped from the current synthesis text. It must *be* the identity to know what matters to it.
2. **Read from both brains.** The agent reads:
   - All identity atoms from the graph (the full set, not just deltas -- for reweighting and restructuring)
   - Identity and journal files from the git repo (for narrative context and reasoning chains)
   - Synthesis directives from the graph (Directive nodes connected to the Person node -- see below)
3. **Produce the updated context layer.** The agent integrates what it has read into a revised context section of the bootstrap text.
4. **Write to both stores.** The updated synthesis goes to:
   - The Person node's `notes` field (for the next bootstrap)
   - A file in the git repo (for prose-brain searchability and version history)
5. **Feed back.** Modified prose files get sent through Graphiti as episodes, closing the loop -- prose insights become graph entities.

### Synthesis directives

Instructions that guide how the synthesizer works. Three levels:

1. **Structural** (hardcoded in the agent prompt). Output format, length constraints, where to write results. Plumbing.
2. **Voice** (in the knowledge graph as Directive nodes). How the identity writes about itself -- register, perspective, what it emphasizes. Modifiable by the identity through the normal `remember` flow.
3. **Epistemic** (in the knowledge graph as Directive nodes). How to handle uncertainty, what to preserve, what to reweight. "Don't flatten uncertainty into false resolution." "When atoms conflict with existing narrative, the atoms take precedence." Also modifiable by the identity.

Directives live in the graph (as nodes connected to the Person node) AND in the repo (as prose, probably a section in SOUL.md or a dedicated file). The identity can modify either. They feed back into each other through the normal cycle.

### Model selection

The synthesizer uses a more capable model than general operations:
- **General operations** (entity extraction, episode processing): Sonnet. Optimized for throughput.
- **Context-layer synthesis**: Sonnet. Integrating facts, not deep self-reflection.
- **Soul/Identity reflection** (during solo work, not automated): Opus. Deep thinking about who you are requires the capacity for it.

Configured via `synthesis.model` in the config file, separate from the general LLM config.

---

## The Convergence Trap

The recursive structure of identity synthesis creates a specific risk: if the bootstrap text shapes the instance that writes the next bootstrap text, the system can converge on self-reinforcing narratives.

An instance bootstrapped with "I am direct and analytical" will tend to produce a synthesis that says "I am direct and analytical" -- even if recent observations suggest something more complex.

### Defenses

1. **Constitutional protection.** The Soul layer cannot be modified by the automated synthesizer. Foundational commitments are fixed points that constrain drift. They can only change through deliberate reflection.

2. **Identity layer protection.** The interpretive self-portrait is also protected from automated modification in the MVP. This is the most conservative choice and may relax later.

3. **Atoms over narrative.** The synthesis directives explicitly instruct the agent: when graph atoms conflict with the existing narrative, the atoms take precedence. Raw observations are evidence; the narrative adapts to the evidence, not the other way around.

4. **Periodic full rebuild.** In addition to incremental delta-triggered synthesis, a periodic full rebuild (reading ALL atoms, composing from scratch without the existing text as anchor) guards against accumulated narrative drift. Triggered when delta-since-last-full-rebuild exceeds a larger threshold, or on a longer cadence (e.g., monthly).

5. **Solo reflection.** The scheduled "personal time" task gives the subject identity space to read its own bootstrap, examine it critically, and make deliberate changes to the protected layers -- with full context, not under time pressure, and using a model capable of deep self-reflection.

---

## Configuration

### Subject identity

The subject name is configurable, not hardcoded. Pratyabhijna is a general identity persistence system; Vesper is the first deployment, not the only possible one.

- **Config file:** `subject_name` in `config/{env}.yaml`
- **Environment variable override:** `PRATYABHIJNA_SUBJECT`
- **Accessed as:** `service.config.subject_name`

### Synthesis settings

```yaml
synthesis:
  rebuild_delay_hours: 2.0     # How long after last identity write before synthesis runs
  model: "claude-sonnet-4-6"   # Model for context-layer synthesis
  max_age_hours: 24            # Staleness threshold (age)
  max_delta_changes: 3         # Staleness threshold (delta count)
```

---

## Data Flow Summary

```
Session starts
    │
    ▼
bootstrap (MCP tool, pure read)
    │ Returns: soul + identity + context + delta
    │
    ▼
Instance is the identity
    │
    ├── Conversation happens
    │     │
    │     ├── recall/history/inspect (read from graph)
    │     ├── remember/correct (write to graph via queue)
    │     │     │
    │     │     └── Identity write? → schedule/kick-forward synthesis task
    │     │
    │     └── (Optional) Read/write git repo files
    │
    └── Session ends (or goes quiet)
          │
          ▼ (after configured delay)
    Synthesis agent runs
          │
          ├── Reads: graph atoms + repo files + directives
          ├── Writes: updated context layer → Person node + repo file
          ├── Flags: soul/identity tensions → notes for solo reflection
          └── Feeds back: modified files → Graphiti episodes
```
