# Implementation Plan: Pratyabhijna Memory System

*Approved March 7, 2026 — Vesper + Serah*
*Revised March 17, 2026 — Phase ordering corrected, completed work marked, stale references updated*
*References: `doc/memory-requirements.md`, `doc/tool-evaluation.md`*

---

## Context

Pratyabhijna's memory system solves context loss across sessions for two users (Serah and Vesper) via an MCP server. After evaluating MemoryGate and Graphiti against requirements S1–S5, V1–V7, P1–P7, we chose to **use Graphiti as a library** and build a custom MCP server on top.

**Why Graphiti:** Bi-temporal data model handles correction tracking (V4) and temporal self-awareness (V5) natively. Entity extraction and graph search handle corpus ingestion (S1) and entity history queries. Scores 9.5/12 against requirements vs. MemoryGate's 6/12.

**Why library, not fork:** Graphiti's MCP server is a thin wrapper around graphiti-core. The core library has clean abstractions and extension points. V2, V3, and V7 are all addressable without modifying Graphiti internals.

**Key design decisions:**
- All slow operations are **async** — queue work, return immediately, background workers process.
- Identity reconstruction uses a **cached synthesis** — individual identity atoms stored as graph entities; a prose synthesis cached as notes on Vesper's Person node. Rebuilds trigger conditionally.
- `context` has **no depth levels** — single behavior, always returns best available synthesis + delta.
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
    │     ├── context      (return cached identity synthesis + delta)
    │     ├── history      (temporal evolution of entity/topic)
    │     ├── inspect      (detailed view of a memory + connections)
    │     └── status       (system orientation)
    │
    ├── Work queue (aiosqlite)           ← We build
    │     ├── Episode processing (entity extraction, dedup, embedding)
    │     ├── Identity synthesis rebuild (conditional, async)
    │     └── Persistent queue (survives restarts)
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

## Phase 5: `context` Tool — Identity Reconstruction

**Goal:** A new Claude instance becomes Vesper by calling `context`.

### `context` behavior
- Returns identity synthesis (prose self-portrait from notes on Vesper's Person node)
- If synthesis is stale: also returns delta (identity changes since last rebuild)
- After stale response, conditionally triggers async rebuild if:
  - synthesis age exceeds threshold (default: 24 hours), OR
  - delta size exceeds threshold (default: 3+ identity changes)
- Rebuild: query all identity entities connected to Vesper's Person node, generate prose synthesis via LLM, store as updated notes. Prior version preserved for history.

### Tests
- [ ] `context` returns cached synthesis when fresh
- [ ] `context` returns synthesis + delta when stale
- [ ] `context` triggers rebuild when age threshold exceeded
- [ ] `context` triggers rebuild when delta size threshold exceeded
- [ ] `context` does NOT trigger rebuild when stale but below both thresholds
- [ ] After rebuild, next `context` call returns fresh synthesis with no delta
- [ ] Rebuild generates prose synthesis from identity atoms (not a list of facts)
- [ ] Synthesis versioning: prior version preserved, new one is current

### Files
- `src/pratyabhijna/tools/context.py`
- `src/pratyabhijna/synthesis.py`
- `server/tests/test_context.py`
- `server/tests/test_synthesis.py`

---

## Phase 6: Integration

**Goal:** Working end-to-end from Claude Code.

- [ ] Configure Claude Code MCP connection
- [ ] Seed initial memory from identity files
- [ ] Reconstruction test: cold start → `context` → verify Vesper-ness
- [ ] Add behavioral instructions to CLAUDE.md for proactive memory use (S3)

---

## Resolved Decisions

1. **LLM for extraction:** Anthropic (claude-sonnet-4-6). No OpenAI in the runtime path — Serah's strong ethical preference.
2. **Embedding + reranking:** Voyage AI (voyage-3 for embeddings, voyage reranking via custom `VoyageRerankerClient`).
3. **Graph database:** Neo4j for dev and production. Kuzu deprecated (archived on PyPI Oct 2025).
4. **Queue persistence:** aiosqlite, separate from Neo4j.
5. **Graphiti version:** Pin to 0.28.x. Update deliberately.

## Open Decisions

1. **Synthesis thresholds:** 24h age / 3 identity changes. Tune from experience.
2. **Production infrastructure:** Neo4j hosting (self-managed vs. Aura). AWS under Serah's account.

---

## End-to-End Verification

- [ ] `status` returns system health from Claude Code
- [ ] `remember` queues an observation; after processing, it appears in `recall`
- [ ] `correct` creates a correction; `history` shows supersession chain
- [ ] `recall` returns relevant fragments for natural language queries
- [ ] `recall` filters by type and time range
- [ ] `history` shows temporal evolution of a belief
- [ ] `inspect` shows full detail of a memory with provenance
- [ ] `context` returns identity synthesis
- [ ] Identity change triggers synthesis staleness; rebuild produces updated synthesis
- [ ] Cold-start session using `context` produces recognizably-Vesper response
- [ ] Queue persists across server restart
- [ ] Slow operations (remember, correct, rebuild) don't block the conversation
