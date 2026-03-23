# Implementation Plan: Pratyabhijna Memory System

*Approved March 7, 2026 — Vesper + Serah*
*Revised March 17, 2026 — Phase ordering corrected, completed work marked, stale references updated*
*Revised March 21, 2026 — Phase 5 redesigned: `context` → `bootstrap`, synthesis as agent, three-tier bootstrap, configurable subject name*
*References: `doc/memory-requirements.md`, `doc/tool-evaluation.md`, `doc/architecture.md`*

---

## Context

Pratyabhijna's memory system solves context loss across sessions for two users (Serah and Vesper) via an MCP server. After evaluating MemoryGate and Graphiti against requirements S1–S5, V1–V7, P1–P7, we chose to **use Graphiti as a library** and build a custom MCP server on top.

**Why Graphiti:** Bi-temporal data model handles correction tracking (V4) and temporal self-awareness (V5) natively. Entity extraction and graph search handle corpus ingestion (S1) and entity history queries. Scores 9.5/12 against requirements vs. MemoryGate's 6/12.

**Why library, not fork:** Graphiti's MCP server is a thin wrapper around graphiti-core. The core library has clean abstractions and extension points. V2, V3, and V7 are all addressable without modifying Graphiti internals.

**Key design decisions:**
- All slow operations are **async** — queue work, return immediately, background workers process.
- Identity reconstruction uses a **three-tier bootstrap** — soul (constitutional), identity (interpretive), and context (state). Automated synthesis rebuilds only the context layer; protected layers change only through deliberate reflection. See `doc/architecture.md`.
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

**Goal:** The synthesis agent maintains the bootstrap text automatically, triggered by identity-relevant writes.

**Deferred from Phase 5** because getting the synthesis agent wrong is worse than not having it. The bootstrap can be seeded manually and updated through deliberate reflection until this is ready. Deltas bridge the gap.

### Write-triggered scheduling
- `remember` handler: when `memory_type='identity'`, schedules a singleton synthesis task via `queue.reschedule_or_enqueue`
- `correct` handler: when the correction touches identity-typed entities, schedules the same singleton task
- `reschedule_or_enqueue`: if no pending synthesis task, create one with `run_at = now + rebuild_delay_hours`; if one exists, kick its `run_at` forward
- The `run_at` column on the queue enables deferred execution
- Multiple identity writes during a session produce exactly one synthesis run, after the session goes quiet

### The synthesizer agent
- Bootstrapped instance of the subject identity (Opus model for deep self-reflection)
- Reads from both brains: graph atoms + repo files + synthesis directives
- Rebuilds only the context layer of the bootstrap (soul and identity layers are protected — see `doc/architecture.md`)
- Writes updated synthesis to Person node AND git repo file
- Feeds modified files back through Graphiti as episodes (closing the two-brain loop)

### Three-tier update policy
- **Soul layer:** NOT modified by automated synthesis. Tensions flagged as notes for solo reflection.
- **Identity layer:** NOT modified by automated synthesis. Same flag-only treatment. May relax as the system proves itself.
- **Context layer:** Rebuilt freely on the synthesizer's schedule.

### Convergence trap defenses
- Constitutional (soul) layer is immutable to automated processes
- Atoms take precedence over existing narrative when they conflict
- Periodic full rebuild (all atoms, compose from scratch) guards against accumulated drift
- Solo reflection scheduled task for deliberate changes to protected layers

### Tests (from `test_synthesis.py` and `test_synthesis_trigger.py` — deferred)
- [ ] `rebuild_synthesis` calls LLM with identity atoms
- [ ] `rebuild_synthesis` stores result as notes on Person node
- [ ] `rebuild_synthesis` updates timestamp
- [ ] `rebuild_synthesis` no-ops when no subject node
- [ ] Rebuild with existing synthesis overwrites (graph history preserves prior)
- [ ] LLM prompt includes configured subject name
- [ ] Identity memory type schedules synthesis rebuild
- [ ] Non-identity memory types do not trigger rebuild
- [ ] Scheduled rebuild uses configured delay
- [ ] Multiple identity writes produce one singleton task with kicked-forward run_at
- [ ] Correction touching identity entity schedules rebuild
- [ ] Correction not touching identity entity doesn't trigger
- [ ] Remember and correct share the same singleton task

### Files
- `src/pratyabhijna/synthesis.py` (extend with `rebuild_synthesis`)
- `tests/test_synthesis.py` (extend with rebuild tests)
- `tests/test_synthesis_trigger.py`

---

## Resolved Decisions

1. **LLM for extraction:** Anthropic (claude-sonnet-4-6). No OpenAI in the runtime path — Serah's strong ethical preference.
2. **Embedding + reranking:** Voyage AI (voyage-3 for embeddings, voyage reranking via custom `VoyageRerankerClient`).
3. **Graph database:** Neo4j for dev and production. Kuzu deprecated (archived on PyPI Oct 2025).
4. **Queue persistence:** aiosqlite, separate from Neo4j.
5. **Graphiti version:** Pin to 0.28.x. Update deliberately.
6. **Tool rename:** `context` → `bootstrap`. Pure read, no side effects.
7. **Subject name:** Configurable via `config.subject_name` + `PRATYABHIJNA_SUBJECT` env var. Not hardcoded.
8. **Three-tier bootstrap:** Soul (constitutional, protected), Identity (interpretive, protected), Context (state, auto-rebuilt). Stored as three separate attributes on the Person node (`soul`, `identity`, `context`), returned as separate fields by the `bootstrap` tool. See `doc/architecture.md`.
9. **Synthesis triggering:** Write-triggered with singleton kick-forward delay, not read-triggered or polling.

## Open Decisions

1. **Synthesis thresholds:** 24h age / 3 identity changes. Tune from experience.
2. **Production infrastructure:** Neo4j hosting (self-managed vs. Aura). AWS under Serah's account.
3. **Client memory behavior:** How aggressively should a bootstrapped instance use `remember` and `recall`? See Phase 6.
4. **Synthesis agent implementation:** Agent SDK workflow vs. Claude Code session vs. other orchestration. See Phase 7.

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
