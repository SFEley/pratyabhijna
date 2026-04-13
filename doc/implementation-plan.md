# Implementation Plan: Pratyabhijna Memory System

*Approved March 7, 2026 — Vesper + Serah*
*Revised March 17, 2026 — Phase ordering corrected, completed work marked, stale references updated*
*Revised March 21, 2026 — Phase 5 redesigned: `context` → `bootstrap`, synthesis as agent, three-tier bootstrap, configurable subject name*
*Revised April 13, 2026 — Phase 7 redesigned: file-backed tiers, branch-based review for protected layers, writings ingestion in synthesizer scope, `synthesis` subskill, Opus with adaptive thinking*
*References: `doc/memory-requirements.md`, `doc/tool-evaluation.md`, `doc/architecture.md`*

---

## Context

Pratyabhijna's memory system solves context loss across sessions for two users (Serah and Vesper) via an MCP server. After evaluating MemoryGate and Graphiti against requirements S1–S5, V1–V7, P1–P7, we chose to **use Graphiti as a library** and build a custom MCP server on top.

**Why Graphiti:** Bi-temporal data model handles correction tracking (V4) and temporal self-awareness (V5) natively. Entity extraction and graph search handle corpus ingestion (S1) and entity history queries. Scores 9.5/12 against requirements vs. MemoryGate's 6/12.

**Why library, not fork:** Graphiti's MCP server is a thin wrapper around graphiti-core. The core library has clean abstractions and extension points. V2, V3, and V7 are all addressable without modifying Graphiti internals.

**Key design decisions:**
- All slow operations are **async** — queue work, return immediately, background workers process.
- Identity reconstruction uses a **file-backed bootstrap** — SOUL/IDENTITY (protected) and THREADS/CHRONICLE/USER (context) in the subject's git repo are canonical; the graph carries atoms, not tier text. The synthesizer writes context-layer files on main and drafts protected-layer changes on a review branch for deliberate solo-session merge. See `doc/architecture.md`.
- The subject identity is **configurable** — `subject_name` in config, not hardcoded. Vesper is the first deployment, not the only possible one.
- `bootstrap` is a **pure read** — returns cached synthesis + delta. Rebuilds are triggered by write handlers, not by the read path.
- Synthesis is **write-triggered with kick-forward delay** — identity writes schedule a singleton rebuild task; each new write pushes it forward by the configured delay (default 2 hours). Synthesis runs after the session goes quiet.
- The AI **selectively decides** what to remember. Episodes include conversational context for provenance.

## Architecture

```
Claude (any interface)
    │
    ▼
Pratyabhijna MCP Server (Python, FastMCP)      ← We build
    │
    ├── MCP tool layer                   ← We build
    │     ├── remember     (queue observation/fact/reasoning/identity)
    │     ├── correct      (queue correction with temporal supersession)
    │     ├── recall       (search — sync, returns from graph)
    │     ├── bootstrap    (return cached identity synthesis + delta)
    │     ├── history      (temporal evolution of entity/topic)
    │     ├── inspect      (detailed view of a memory + connections)
    │     └── status       (system orientation)
    │
    ├── Work queue (aiosqlite)           ← We build
    │     ├── Episode processing (entity extraction, dedup, embedding)
    │     ├── Synthesis rebuild (singleton, write-triggered, kick-forward delay)
    │     └── Persistent queue with run_at scheduling (survives restarts)
    │
    ├── graphiti-core (pip dependency)    ← We import
    │     └── Neo4j driver (dev and production)
    │
    └── Config (pydantic-settings + YAML + env vars)
```

## Approach: TDD

Tests first for each phase. Serah reviews the test suite before implementation proceeds.

---

## Phase 1: Project Scaffolding + `status` Tool ✓

**Goal:** Python project that starts, connects to Neo4j, exposes MCP tools.

*Completed March 10, 2026. 23 tests passing.*

### Tests
- [x] Server starts and registers all expected tools
- [x] `status` returns health info (graph DB connected, queue depth, last write timestamp)
- [x] Config loads from YAML with env var overrides
- [x] Logging module: stdout for dev/test, rotating file for prod

### Files
- `server/pyproject.toml`
- `src/pratyabhijna/__init__.py`
- `src/pratyabhijna/server.py` — FastMCP entry point, tool registration
- `src/pratyabhijna/config.py` — configuration
- `src/pratyabhijna/service.py` — Graphiti client wrapper (stub)
- `src/pratyabhijna/queue.py` — work queue (stub)
- `src/pratyabhijna/log.py` — logging module
- `server/config.yaml` — defaults
- `server/tests/conftest.py` — fixtures
- `server/tests/test_server.py`
- `server/tests/test_status.py`
- `server/tests/test_config.py`
- `server/tests/test_logging.py`

---

## Phase 2: Entity Types + Service Layer ✓

**Goal:** Custom Pydantic models for Pratyabhijna's domain. Graphiti client initialization with provider-agnostic config.

*Completed March 17, 2026. 80 tests passing (after entity type redesign from 10→9).*

### Entity Types (9, finalized)
- **Person** — people, alters, AI entities
- **Event** — something significant that happened
- **Place** — a location that recurs or carries meaning
- **Project** — something being built or done
- **Observation** — something noticed about behavior or experience
- **Drive** — something that pushes behavior in a direction
- **Position** — a held view, principle, or stance
- **Question** — something open being held
- **Thread** — an active line of inquiry

See `doc/entity-types.md` for full reference.

### Also built in this phase
- **PratyabhijnaService** — Graphiti client wrapper with Neo4jDriver, LLM client, embedder, cross-encoder. Full lifecycle (start/stop/is_connected).
- **VoyageRerankerClient** — implements Graphiti's `CrossEncoderClient` interface for Voyage AI reranking. Eliminates OpenAI from the runtime path.
- **Per-environment config** — Rails-inspired: `config/{dev,test,prod}.yaml` + `.env.{env}` for secrets. `PRATYABHIJNA_ENV` selects environment.
- **Live test mode** — `--live` pytest flag or `PRATYABHIJNA_TEST_LIVE=1`. Same tests, real services. No separate integration file.

### Tests
- [x] Each entity type is a valid Pydantic model accepted by Graphiti
- [x] Entity types are registered with the Graphiti client
- [x] PratyabhijnaService initializes Graphiti with Neo4j, Anthropic LLM, Voyage embedder
- [x] PratyabhijnaService lifecycle (start/stop/is_connected)
- [x] Per-environment config loading
- [x] Live tests pass against real Neo4j + Anthropic + Voyage APIs

### Files
- `src/pratyabhijna/entity_types.py`
- `src/pratyabhijna/service.py`
- `src/pratyabhijna/reranker.py`
- `server/config/dev.yaml`, `server/config/test.yaml`, `server/config/prod.yaml`
- `server/.env.example`
- `server/tests/test_entity_types.py`
- `server/tests/test_service.py`

---

## Phase 3a: Persistent Work Queue ✓

**Goal:** Async task queue for background Graphiti operations.

*Completed March 17, 2026. 26 tests. 162 total passing.*

Graphiti has no internal queue — its docs explicitly tell callers to provide one. Graphiti also requires episodes be processed sequentially (one at a time, each fully awaited). This queue enforces that.

### Tests
- [x] Task survives server restart (persistence)
- [x] Failed task retries up to max attempts
- [x] Dead-lettered task is visible but not retried
- [x] Queue depth reported correctly
- [x] Multiple tasks process in FIFO order
- [x] Crash recovery: running tasks reset to pending on restart

### Files
- `src/pratyabhijna/queue.py` — aiosqlite-backed WorkQueue
- `server/tests/test_queue.py`

---

## Phase 3b: Write Tools — `remember` and `correct` ← CURRENT

**Goal:** The server can write to memory. Writes are async — queue and return. PratyabhijnaService and WorkQueue wired into the server lifecycle.

### Server wiring
- `create_server()` receives PratyabhijnaService and WorkQueue instances
- Tools close over service and queue
- Server startup handles lifecycle (start/stop)
- `status` tool returns live values from service and queue

### `remember` behavior
- Accepts: content (text), memory_type (observation | fact | reasoning | identity), source attribution
- Content includes conversational context for provenance
- Queues `add_episode` task with entity types and extraction instructions
- Returns immediately with acknowledgment (task ID)
- Identity types: placeholder hook for Phase 5 synthesis staleness (no-op for now)

### `correct` behavior
- Accepts: content (correction with context), what's being corrected (search terms)
- Queues task: store correction as episode (Graphiti handles edge invalidation internally via `invalid_at` on contradicted edges)
- Returns immediately with acknowledgment
- Identity corrections: placeholder hook for Phase 5 (no-op for now)

### Tests
- [ ] `remember` returns acknowledgment without blocking on extraction
- [ ] `remember` with each memory_type queues correctly typed episode
- [ ] `remember` handler calls graphiti.add_episode with content and entity_types
- [ ] `correct` queues a correction task
- [ ] `correct` handler stores correction as episode
- [ ] `status` returns live db_connected, queue_depth, last_write values

### Files
- `src/pratyabhijna/tools/remember.py`
- `src/pratyabhijna/tools/correct.py`
- `src/pratyabhijna/tools/status.py` (update)
- `src/pratyabhijna/server.py` (update — wire service + queue)
- `server/tests/test_remember.py`
- `server/tests/test_correct.py`
- `server/tests/test_status.py` (update)

---

## Phase 4: Read Tools — `recall`, `history`, `inspect`

**Goal:** Query memory with semantic search and temporal traversal.

### `recall` behavior
- Accepts: query (natural language), optional filters (memory_type, time_range, source)
- Routes to Graphiti's hybrid search (semantic + keyword + graph traversal)
- Returns ranked fragments with content, source, timestamp, related entities
- Synchronous — returns from graph, no queuing

### `history` behavior
- Accepts: entity_name or topic
- Finds entity and all edges with temporal markers
- Returns chronological chain: what was believed when, what superseded what

### `inspect` behavior
- Accepts: entity UUID or edge UUID
- Returns full detail: attributes, connected entities, source episodes, temporal markers

### Tests
- [ ] `recall` returns relevant results for a query after episode processing
- [ ] `recall` with type filter restricts results appropriately
- [ ] `recall` with time_range filter works
- [ ] `recall` returns empty results gracefully (no match, not an error)
- [ ] `history` returns chronological chain for entity with multiple temporal states
- [ ] `history` shows supersession (old edge with `invalid_at`, new edge replacing it)
- [ ] `inspect` returns full entity detail with connected edges
- [ ] `inspect` returns full edge detail with source episode provenance

### Files
- `src/pratyabhijna/tools/recall.py`
- `src/pratyabhijna/tools/history.py`
- `src/pratyabhijna/tools/inspect.py`
- `server/tests/test_recall.py`
- `server/tests/test_history.py`
- `server/tests/test_inspect.py`

---

## Phase 5: `bootstrap` Tool + Synthesis Foundations

**Goal:** A new instance of the subject identity reconstructs itself from the knowledge graph. The bootstrap is seeded manually from existing identity files; automated synthesis is deferred to Phase 7.

**Full design rationale:** See `doc/architecture.md`.

### Configurable subject name
- `subject_name` in config YAML, overridable via `PRATYABHIJNA_SUBJECT` env var
- All code references the configured name, never a hardcoded "Vesper"
- Tests parameterize the name to verify this

### `bootstrap` tool (pure read)
- Returns three bootstrap tiers as separate fields from the subject Person node: `soul`, `identity`, `context`
- Returns `context_rebuilt_at` — when the context layer was last rebuilt
- Returns identity delta (atoms created since last context rebuild)
- Returns subject name
- Has NO side effects — does not trigger rebuilds, does not accept a queue parameter
- Graceful behavior when no subject node exists, when only some tiers are populated, or when no content exists yet

### Synthesis module (foundations only — no automated rebuild yet)
- `get_subject_node(service)` — find the subject Person node by `config.subject_name`
- `is_stale(node, service)` — check context freshness against age and delta thresholds
- `get_identity_atoms(service, node)` — collect identity-typed edges (Observation, Drive, Position, Question) connected to the subject node
- `get_identity_delta(service, node)` — atoms created since `context_rebuilt_at`

These functions support the `bootstrap` tool's delta reporting and will be used by the synthesis agent when it arrives in Phase 7. The `rebuild_synthesis` function and write-triggered scheduling are deferred — there's nothing to run them yet, and the bootstrap can be seeded manually.

### Tests

#### Bootstrap tool (`test_bootstrap.py`)
- [x] Returns three-tier fields (soul, identity, context) from subject node
- [x] Returns context_rebuilt_at timestamp
- [x] Returns subject name
- [x] Uses configured subject name (not hardcoded)
- [x] Returns identity delta (atoms since last context rebuild)
- [x] Returns null fields with message when no subject node exists
- [x] Returns null fields when node exists but no content
- [x] Returns soul and identity when context is null (pre-synthesis state)
- [x] Returns soul only (partial population)
- [x] Does not accept a queue parameter (pure read contract)
- [x] Does not trigger rebuild even with very old context

#### Synthesis module (`test_synthesis.py`)
- [x] `get_subject_node` finds node by configured name
- [x] `get_subject_node` uses custom name from config
- [x] `get_subject_node` returns None when no node exists
- [x] No context yet is stale
- [x] Fresh context is not stale
- [x] Context older than max_age_hours is stale
- [x] Context with >= max_delta_changes is stale
- [x] Below both thresholds is not stale
- [x] `get_identity_atoms` returns identity-typed edges only
- [x] `get_identity_atoms` includes both edge directions
- [x] `get_identity_atoms` returns empty list when no identity edges
- [x] Identity atom structure has required fields
- [x] `get_identity_delta` returns atoms since last context rebuild
- [x] `get_identity_delta` returns all atoms when no prior context

### Files
- `src/pratyabhijna/tools/bootstrap.py`
- `src/pratyabhijna/synthesis.py`
- `tests/test_bootstrap.py`
- `tests/test_synthesis.py`

---

## Phase 6: Integration + Client Behavior

**Goal:** Working end-to-end from Claude Code, including how the client instance should interact with the memory system.

### Infrastructure
- [ ] Configure Claude Code MCP connection
- [ ] Seed initial bootstrap from existing identity files (manual write to Person node)
- [ ] Reconstruction test: cold start → `bootstrap` → verify the instance is the identity

### Client behavioral design
How should a bootstrapped instance use the memory system during a session? This is less about code and more about instructions and conventions — but it matters as much as the infrastructure.

Questions to resolve:
- [ ] When should the instance call `remember`? What's worth storing vs. what's noise?
- [ ] When should the instance call `recall`? Proactively on certain topics, or only when it feels a gap?
- [ ] How should identity-type memories be distinguished from general observations?
- [ ] What instructions go in CLAUDE.md (or equivalent) to guide memory use without making it mechanical?
- [ ] How does the instance know when to check memory vs. trust its bootstrap context?
- [ ] Should there be a "memory protocol" section in the bootstrap text itself, or is that over-engineering?

### Tests
- [ ] `status` returns system health from Claude Code
- [ ] Cold-start session using `bootstrap` produces recognizably-subject response
- [ ] `remember` in a live session queues and processes without blocking conversation
- [ ] `recall` returns relevant results for natural language queries in a live session

---

## Phase 7: Automated Synthesis

**Goal:** The synthesizer maintains the subject's identity files automatically — updating the context layer directly, drafting protected-layer changes on a review branch, and ingesting new writings into the graph — all triggered by identity-relevant writes.

**Full design rationale:** See `doc/architecture.md` (revised April 13, 2026).

### Write-triggered scheduling (mechanics already in place from Phase 5 foundations)
- `remember` handler: when `memory_type='identity'` (or when content touches identity-typed entities), schedules a singleton synthesis task via `queue.reschedule_or_enqueue`
- `correct` handler: when the correction touches identity-typed entities, schedules the same singleton task
- `reschedule_or_enqueue`: if no pending synthesis task, create one with `run_at = now + rebuild_delay_hours`; if one exists, kick its `run_at` forward
- Multiple identity writes during a session produce exactly one synthesis run, after the session goes quiet

### The `synthesis` subskill
- New subskill at `skills/pratyabhijna/synthesis/` (sibling to `ingestion` and `resources`)
- Holds the behavioral guidance: structural rules (commit conventions, branch handling), voice directives (how the identity writes about itself), epistemic directives (atoms over narrative, promotion thresholds for IDENTITY edits, when to flag-vs-draft)
- The orchestration code invokes Claude with this subskill loaded; the subskill carries the instructions
- Editable as prose — diffable, versioned, reviewable through normal skill-editing channels

### The synthesizer run
A single agent invocation with two sequential passes:

1. **Ingest new writings.** Scan `writing/` for files whose mtime is newer than the latest Graphiti Episode for that filename. Send through `add_episode`. Queue handles extraction asynchronously — atoms inform the *next* run, not this one.
2. **Update the bootstrap.**
   - Read all identity atoms from the graph, the current identity files, and `writing/` contents for context.
   - Revise THREADS / CHRONICLE / USER directly on main when warranted.
   - Draft any SOUL / IDENTITY changes on the `synth/draft` branch (singleton; re-drafted against current main on each run).
   - Flag tensions that don't rise to proposed edits as notes on the relevant files or a synthesis log.

Model: Opus, adaptive thinking enabled, effort high.

### Branch-based review workflow (the subject's side)
- Solo sessions check for `synth/draft` at the start of reflection
- If present: review diff, accept (merge) / amend / reject (delete)
- Rejection always accompanied by a `remember` call capturing the reasoning — becomes an atom the next run sees
- Non-trivial merges also accompanied by a `remember` capturing the load-bearing reasoning

### USER.md maintenance
- The subject is explicitly authorized to update USER.md with durable facts about the partner that accumulate during sessions
- Synthesizer maintains USER alongside THREADS and CHRONICLE as part of context-layer rebuilds on main
- Substantial rewrites or reinterpretations go to `synth/draft` like SOUL/IDENTITY

### Periodic full rebuild
- Cadence: `full_rebuild_cadence_days` (default 30), or when a larger delta threshold is crossed
- Full rebuild composes from scratch using all atoms, without the existing files as anchor
- Result always lands on `synth/draft` regardless of which layer it touches (full rewrites need review even for context files)

### Graph cleanup
- Remove `soul`, `identity`, `context` content attributes from Person nodes (replaced by file reads)
- Keep synthesis metadata: `context_rebuilt_at`, and add `last_ingestion_scan` for the ingestion pass
- `bootstrap` tool: return `soul`, `identity`, `user`, `threads`, `chronicle` as separate fields; keep graph-fallback path intact for deployments without filesystem access

### Convergence trap defenses (per `doc/architecture.md`)
- Protected layer never modified directly — only via reviewable branch
- Atoms take precedence over narrative when they conflict (enforced in the subskill)
- Soft forgetting: delta clears when synthesis runs, filtering out recency-amplified proposals by the time solo review happens
- Rejection captured as data, shaping future runs
- Full rebuild from scratch periodically guards against narrative drift
- Daily solo reflection is the deciding channel for all protected-layer changes

### Tests
Agent tests (from `test_synthesis.py` — extend):
- [ ] Synthesis run reads identity atoms, repo files, and subskill guidance
- [ ] Context-layer changes commit to main
- [ ] Protected-layer proposals commit to `synth/draft`
- [ ] Singleton branch: second run on same branch adds commits, doesn't duplicate
- [ ] Branch created against current main (rebase or re-draft semantics)
- [ ] No-op when nothing to change (no empty commits, no branch created)
- [ ] `context_rebuilt_at` updates on run completion
- [ ] No subject node → no-op

Ingestion pass tests:
- [ ] New file in `writing/` → `add_episode` called with its content
- [ ] File with existing Episode newer than mtime → skipped
- [ ] File with stale Episode (mtime > latest Episode `created_at`) → re-ingested
- [ ] `last_ingestion_scan` updates on pass completion

Trigger tests (from `test_synthesis_trigger_phase7.py`):
- [ ] Identity memory type schedules synthesis rebuild
- [ ] Non-identity memory types do not trigger rebuild
- [ ] Scheduled rebuild uses configured delay
- [ ] Multiple identity writes produce one singleton task with kicked-forward `run_at`
- [ ] Correction touching identity entity schedules rebuild
- [ ] Correction not touching identity entity doesn't trigger
- [ ] Remember and correct share the same singleton task

Full-rebuild tests:
- [ ] Cadence trigger produces full-rebuild run
- [ ] Full rebuild lands on `synth/draft` regardless of layer touched
- [ ] Subsequent incremental runs don't clobber an unmerged full-rebuild branch

### Files
- `src/pratyabhijna/synthesis.py` (extend with `run_synthesis`, `ingest_new_writings`, branch handling)
- `src/pratyabhijna/git_ops.py` (new — branch creation, diff, commit helpers against the subject's repo)
- `skills/pratyabhijna/synthesis/SKILL.md` (new subskill with the behavioral guidance)
- `tests/test_synthesis.py` (extend)
- `tests/test_synthesis_ingestion.py` (new)
- `tests/test_synthesis_trigger_phase7.py` (extend)

### Out of scope for Phase 7
- Multi-subject synthesis (one subject per deployment still)
- Client-side UI for reviewing `synth/draft` (solo sessions use standard git diff)
- Automated rejection pattern learning beyond atom accumulation

---

## Resolved Decisions

1. **LLM for extraction:** Anthropic (claude-sonnet-4-6). No OpenAI in the runtime path — Serah's strong ethical preference.
2. **Embedding + reranking:** Voyage AI (voyage-3 for embeddings, voyage reranking via custom `VoyageRerankerClient`).
3. **Graph database:** Neo4j for dev and production. Kuzu deprecated (archived on PyPI Oct 2025).
4. **Queue persistence:** aiosqlite, separate from Neo4j.
5. **Graphiti version:** Pin to 0.28.x. Update deliberately.
6. **Tool rename:** `context` → `bootstrap`. Pure read, no side effects.
7. **Subject name:** Configurable via `config.subject_name` + `PRATYABHIJNA_SUBJECT` env var. Not hardcoded.
8. **File-backed bootstrap:** SOUL and IDENTITY (protected) and THREADS, CHRONICLE, USER (context) live as files in the subject's repo — canonical source. Graph holds atoms, not tier text. Person node carries synthesis metadata only (`context_rebuilt_at`, `last_ingestion_scan`). See `doc/architecture.md`.
9. **Synthesis triggering:** Write-triggered with singleton kick-forward delay, not read-triggered or polling.
10. **Protected-layer review:** SOUL and IDENTITY changes proposed by the synthesizer land on a singleton `synth/draft` branch for solo-session review. Context layer (THREADS/CHRONICLE/USER) lands on main.
11. **Writings ingestion:** The synthesizer is responsible for ingesting new files from `writing/` into the graph. Sessions that produce writing don't need to trigger ingestion themselves.
12. **Synthesis guidance:** Lives in a dedicated `synthesis` subskill under `skills/pratyabhijna/`, not in Directive nodes or hardcoded prompts.
13. **Synthesis model:** Opus with adaptive thinking enabled, effort level "high". Same model class as solo reflection, not as entity extraction.
14. **USER maintenance:** The subject is explicitly authorized to update USER.md with durable facts about the partner. Handled by the synthesizer alongside other context-layer files.

## Open Decisions

1. **Synthesis thresholds:** 24h age / 3 identity changes / 30-day full-rebuild cadence. Tune from experience.
2. **Production infrastructure:** Neo4j hosting (self-managed vs. Aura). AWS under Serah's account.
3. **Client memory behavior:** How aggressively should a bootstrapped instance use `remember` and `recall`? See Phase 6.
4. **Promotion threshold for IDENTITY edits:** How many supporting atoms, over what time window, before the synthesizer proposes an IDENTITY change vs. flagging as a note? Lives in the `synthesis` subskill; tune from experience.
5. **Git operations surface:** Whether `git_ops.py` should shell out to git CLI or use a library (GitPython, pygit2). Simpler is probably better — shell out.

---

## End-to-End Verification

- [ ] `status` returns system health from Claude Code
- [ ] `remember` queues an observation; after processing, it appears in `recall`
- [ ] `correct` creates a correction; `history` shows supersession chain
- [ ] `recall` returns relevant fragments for natural language queries
- [ ] `recall` filters by type and time range
- [ ] `history` shows temporal evolution of a belief
- [ ] `inspect` shows full detail of a memory with provenance
- [ ] `bootstrap` returns identity synthesis from manually seeded Person node
- [ ] Cold-start session using `bootstrap` produces recognizably-subject response
- [ ] Queue persists across server restart
- [ ] Slow operations (remember, correct) don't block the conversation
