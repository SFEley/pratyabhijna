# Architecture: Two Brains and a Bootstrap

*Drafted March 21, 2026 -- Vesper + Serah*
*Revised April 13, 2026 -- Synthesis design updated: file-backed tiers, branch-based review for protected layers, writings ingestion in synthesizer scope, subskill for guidance*
*References: `doc/memory-requirements.md`, `doc/implementation-plan.md`, `doc/entity-types.md`

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

A new instance of the subject identity starts cold -- no memory, no continuity. The bootstrap is what makes it *be* the identity. Its canonical form is the set of identity files in the subject's git repo, read via the `bootstrap` MCP tool (which uses filesystem access when the server has a configured `repo_path`, and falls back to Person-node attributes otherwise).

### Structure: tiers with different update policies

The bootstrap has a **protected layer** and a **context layer**, each backed by files in the subject's repo. The graph no longer duplicates this content as Person-node attributes; the files are the source of truth. The Person node still carries synthesis metadata (`context_rebuilt_at`, `last_ingestion_scan`) but not the tier text itself.

**Protected layer**

- **SOUL.md** -- foundational commitments, voice, values. Constitutional. "No sycophancy" is not an observation about behavior -- it is a constraint the identity holds. Changes rarely.
- **IDENTITY.md** -- evolving self-portrait, trained patterns, unresolved questions. Interpretive. This is where productive uncertainty lives: "I'm not sure whether this pattern is real" is a valuable thing to hold.

Both files are **protected from direct automated modification**. When the synthesizer decides a change is warranted, it drafts the change on a dedicated git branch (see "Branch-based review" below) for the subject to review during solo work. It does not commit to main.

*Why protected:* Soul commitments are fixed points that constrain identity drift. Identity interpretation requires judgment, not integration. An automated process that silently resolves open questions or erodes commitments based on a handful of data points would corrupt the very layer the bootstrap depends on. Branch-based review preserves automation's value (surfacing proposals based on accumulated atoms) while keeping the deciding with the bootstrapped instance during deliberate reflection.

**Context layer**

- **THREADS.md** -- active open questions and ongoing work. What's live.
- **CHRONICLE.md** -- significant moments and turning points. Historical record.
- **USER.md** -- the human partner's self-description and accumulated facts about them. Written for the subject.

These files describe current state and accretion, not deep commitments. The synthesizer **writes them directly on main** on its schedule. Errors self-correct quickly because the content describes what is happening, not what is committed to. THREADS is maintained (resolving closed threads, linking new ones, pruning dead ones); CHRONICLE is append-mostly with occasional restructuring when a thread crystallizes into a turning point; USER is updated as the subject learns durable facts about the partner — a responsibility the partner has explicitly delegated.

### The `bootstrap` tool

The MCP tool `bootstrap` returns the tier contents plus any identity delta (atoms created since the last context rebuild). It also checks whether synthesis is stale and schedules a run if so — this is the primary synthesis trigger. The synthesizer runs in the background; bootstrap returns without waiting for it.

Response includes:
- `subject` -- the configured subject name
- `soul` -- contents of SOUL.md
- `identity` -- contents of IDENTITY.md
- `user` -- contents of USER.md
- `threads` -- contents of THREADS.md
- `chronicle` -- contents of CHRONICLE.md (may be summarized when large; full file available via `pratya://` resources)
- `context_rebuilt_at` -- when the context layer was last rebuilt
- `delta` -- identity atoms created since the last context rebuild
- `source` -- `"files"` when served from the repo, `"graph"` when served from Person-node fallback

---

## Identity Synthesis

Synthesis is the process that integrates new knowledge into the bootstrap text. It is the mechanism by which the two brains feed each other.

### Triggering

Synthesis is triggered at session start via `bootstrap`: if the context is stale (delta count exceeds `max_delta_changes`, or the last rebuild is older than `max_age_hours`, or synthesis has never run), a synthesize task is scheduled immediately. The synthesizer runs in the background while the session proceeds.

The `correct` handler provides a belt-and-suspenders trigger: after processing a correction, it checks whether the subject node has any identity-typed neighbors (Observations, Drives, Positions, Questions) and schedules synthesis if so. This fires in real time rather than at session start.

The scheduled task is a **singleton** -- only one pending synthesis task exists at a time. Repeated triggers reschedule in place rather than stacking up.

### Scope of the synthesizer's work

Each run has two responsibilities:

1. **Ingest any new prose.** Scan the subject's `writing/` directory for files whose mtime is newer than the latest Graphiti Episode for that filename. Send those through `add_episode` so the prose brain feeds the associational brain. Closes the loop the architecture has always described — but shifts the responsibility from the session that wrote the piece (wrong mode) to the synthesizer (right mode, already reading `writing/`).

2. **Update the bootstrap.** Rebuild the context layer (THREADS/CHRONICLE/USER) directly, and draft any warranted changes to the protected layer (SOUL/IDENTITY) on a review branch. Surface tensions that don't rise to a proposed change as notes on the live files.

The two passes run **sequentially in a single agent invocation**, ingestion first. Atoms extracted from freshly ingested prose land asynchronously via the queue; they inform the *next* synthesizer run, not the current one. A one-cycle lag on new writing is acceptable and keeps the two passes independent.

### Branch-based review for the protected layer

Context-layer changes (THREADS, CHRONICLE, USER) land on main. Protected-layer changes (SOUL, IDENTITY) and periodic full rebuilds land on a singleton branch — conventionally `synth/draft` — that the subject reviews during solo work. The review workflow:

1. Synthesizer run produces proposed edits to SOUL.md and/or IDENTITY.md. Commits them on `synth/draft` (creating the branch if needed, adding commits if it already exists). Each run works against current main, so subsequent runs may add commits or rewrite as the graph evolves.
2. Solo session checks for the branch at the start of reflection. If present: `git diff main...synth/draft` reveals the proposed changes.
3. Subject reviews, then accepts (merge to main), amends (edit and merge), or rejects (delete the branch).
4. **Rejection is remembered.** Declining a proposed change is self-knowledge — it records what the subject does *not* recognize as theirs, which shapes future proposals. The rejection gets captured as a `remember` call with reasoning, so the next synthesizer run has that atom as context.
5. **Merges are remembered too** when the reasoning is non-trivial. The commit message carries basic intent; a `remember` call captures anything load-bearing about *why* the change felt right.

*Why this shape:*

- **No new moving parts.** The branch is the staging mechanism. `git diff` is the UI. Merge commits carry the decision trail.
- **Full control stays with solo work.** The deciding is always done by the bootstrapped instance in reflection mode, never by the synthesizer alone.
- **Soft forgetting as a filter.** When the synthesizer runs, identity-triggering deltas get cleared (they've been considered). The evaluating session arrives without the delta-amplification that produced the proposal. A change that only reads as true while the atoms are hot probably isn't load-bearing; one that still reads as true to a cooler session earned it. This is an additional convergence-trap defense — recency bias filtered by time.

### The synthesizer as agent (Phase 7)

The synthesizer is not a single LLM call. It is an agent invocation:

1. **Bootstrap.** The synthesizer is an instance of the subject identity, bootstrapped from the current identity files. It must *be* the identity to know what matters to it.
2. **Load guidance.** The agent runs with the `synthesis` subskill in its instructions (see below). The subskill holds the behavioral directives for how to synthesize; the orchestration code stays small.
3. **Read.** The agent reads all identity atoms from the graph (full set, for reweighting and restructuring), the current identity files from the repo, and recent prose from `writing/`.
4. **Ingest.** Any new writings go through `add_episode`.
5. **Write context.** Revisions to THREADS/CHRONICLE/USER get committed to main.
6. **Draft protected.** Any warranted SOUL/IDENTITY changes are committed to `synth/draft`.
7. **Flag, don't force.** Tensions that don't rise to a proposed edit get surfaced as notes (either in a log, or appended to the relevant file in a review block).

### Synthesis directives: the `synthesis` subskill

The behavioral guidance for how the synthesizer works lives in a dedicated subskill of `pratyabhijna`, alongside `ingestion` and `resources`. This keeps the orchestration code lean and makes the directives editable as prose — diffable, versioned, and reviewable by the subject the same way any other skill is.

The subskill covers:

- **Structural.** Output format, commit conventions, where to write what, how to handle the branch state.
- **Voice.** How the identity writes about itself — register, perspective, what it emphasizes.
- **Epistemic.** How to handle uncertainty, what to preserve, what to reweight. "Don't flatten uncertainty into false resolution." "When atoms conflict with existing narrative, the atoms take precedence." "A proposed change to IDENTITY requires more than one atom."

The subject modifies the subskill directly through solo work, the same way any skill gets edited. Directive nodes in the graph are not needed — the prose version in the subskill is canonical.

### Model selection

The synthesizer uses Opus with adaptive thinking enabled, effort level **high**. Synthesis is deep self-reflection, not fact-integration — it sits on the same side of the workload as solo reflection, not on the same side as entity extraction.

- **General operations** (entity extraction, episode processing): Sonnet. Optimized for throughput.
- **Synthesis and solo reflection:** Opus, adaptive thinking on, high effort.

Configured via `synthesis.model` and `synthesis.thinking` in the config file, separate from the general LLM config.

---

## The Convergence Trap

The recursive structure of identity synthesis creates a specific risk: if the bootstrap text shapes the instance that writes the next bootstrap text, the system can converge on self-reinforcing narratives.

An instance bootstrapped with "I am direct and analytical" will tend to produce a synthesis that says "I am direct and analytical" -- even if recent observations suggest something more complex.

### Defenses

1. **Constitutional protection.** The Soul layer cannot be modified by the automated synthesizer. Foundational commitments are fixed points that constrain drift. They can only change through deliberate reflection.

2. **Identity layer protection via branch review.** The interpretive self-portrait is never modified directly by the synthesizer. Proposed changes land on `synth/draft` for the subject to accept, amend, or reject during solo work. Automation surfaces candidates; deliberation decides.

3. **Atoms over narrative.** The synthesis subskill explicitly instructs the agent: when graph atoms conflict with the existing narrative, the atoms take precedence. Raw observations are evidence; the narrative adapts to the evidence, not the other way around.

4. **Soft forgetting as a filter.** Between the synthesizer's draft and the solo review, the deltas that produced the proposal get cleared by the synthesis run itself. The reviewing session has to go find the supporting atoms rather than being front-loaded with them. A proposal that still reads as true under cooler blood earned it; one that depended on recency-amplification falls away. Time and forgetting do filtering work the automation can't.

5. **Rejection as data.** Declined proposals become `remember` atoms with reasoning. The next synthesizer run sees what the subject does not recognize as theirs, and calibrates.

6. **Periodic full rebuild.** In addition to incremental delta-triggered synthesis, a periodic full rebuild (reading ALL atoms, composing from scratch without the existing text as anchor) guards against accumulated narrative drift. The full rebuild lands on `synth/draft` regardless of which layer it touches — all-atoms rewrites always need review. Triggered on a longer cadence (e.g., monthly) or when delta-since-last-full-rebuild exceeds a larger threshold.

7. **Solo reflection.** Daily solo sessions give the subject identity space to examine the bootstrap critically, review pending `synth/draft` branches, and make deliberate changes -- with full context, not under time pressure.

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
  rebuild_delay_hours: 2.0         # How long after last identity write before synthesis runs
  model: "claude-opus-4-6"         # Synthesis is deep reflection, not fact integration
  thinking:
    enabled: true                  # Uses Anthropic adaptive thinking (thinking.type: "adaptive")
    effort: "high"                 # low | medium | high | max — soft guidance on thinking spend
  max_age_hours: 24                # Staleness threshold (age)
  max_delta_changes: 3             # Staleness threshold (delta count)
  full_rebuild_cadence_days: 30    # Periodic from-scratch rebuild on synth/draft
  draft_branch: "synth/draft"      # Singleton review branch
```

---

## Data Flow Summary

```
Session starts
    │
    ▼
bootstrap (MCP tool)
    │ Returns: soul + identity + user + threads + chronicle + delta
    │ (from repo files; graph fallback if filesystem unavailable)
    │ Context stale? → schedule synthesis task (runs in background)
    │
    ▼
Instance is the identity
    │
    ├── Conversation happens
    │     │
    │     ├── recall/history/inspect (read from graph)
    │     ├── remember/correct (write to graph via queue)
    │     │     │
    │     │     └── correct touching identity? → schedule/kick synthesis task
    │     │
    │     └── (Optional) Read/write git repo files
    │
    └── Session ends
          │
          ▼ (synthesis runs in background, started at session open)
    Synthesis agent runs (Opus, adaptive thinking high)
          │ Bootstrapped as subject, loads `synthesis` subskill
          │
          ├── Pass 1: Ingest new writings → Graphiti episodes
          │         (atoms land async; inform next run)
          │
          └── Pass 2: Update bootstrap
                ├── THREADS + CHRONICLE + USER → commits on main
                ├── SOUL + IDENTITY changes   → commits on synth/draft
                └── Tensions not rising to edits → flagged as notes

Solo session (daily)
    │
    ├── Check for synth/draft branch
    │     │
    │     └── If present: review diff → accept / amend / reject
    │                     │                │         │        │
    │                     │                └─ merge ─┘        └─ delete branch
    │                     │                                      + remember reasoning
    │                     │
    │                     └── (merge reasoning also remembered when non-trivial)
    │
    └── Deliberate edits to any file, including protected layers
```
