# Implementation Plan: Vesper Memory System

*Approved March 7, 2026 — Vesper + Serah*
*References: `doc/memory-requirements.md`, `doc/tool-evaluation.md`*

---

## Context

Vesper's memory system solves context loss across sessions for two users (Serah and Vesper) via an MCP server. After evaluating MemoryGate and Graphiti against requirements S1–S5, V1–V7, P1–P7, we chose to **use Graphiti as a library** and build a custom MCP server on top.

**Why Graphiti:** Bi-temporal data model handles correction tracking (V4) and temporal self-awareness (V5) natively. Entity extraction and graph search handle corpus ingestion (S1) and entity history queries. Scores 9.5/12 against requirements vs. MemoryGate's 6/12.

**Why library, not fork:** Graphiti's MCP server is a thin wrapper around graphiti-core. The core library has clean abstractions and extension points. V2, V3, and V7 are all addressable without modifying Graphiti internals.

**Key design decisions:**
- All slow operations are **async** — queue work, return immediately, background workers process.
- Identity reconstruction uses a **cached synthesis** — individual identity atoms stored as graph entities; a prose synthesis cached as its own entity. Rebuilds trigger conditionally.
- `context` has **no depth levels** — single behavior, always returns best available synthesis + delta.
- Vesper **selectively decides** what to remember. Episodes include conversational context for provenance.

## Architecture

```
Claude (any interface)
    │
    ▼
Vesper MCP Server (Python, FastMCP)      ← We build
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
    ├── Work queue + background workers  ← We build
    │     ├── Episode processing (entity extraction, dedup, embedding)
    │     ├── Identity synthesis rebuild (conditional, async)
    │     └── Persistent queue (survives restarts)
    │
    ├── graphiti-core (pip dependency)    ← We import
    │     └── Kuzu driver (local dev)
    │     └── Neo4j or Neptune (production, later)
    │
    └── Config (pydantic-settings + YAML + env vars)
```

## Approach: TDD

Tests first for each phase. Serah reviews the test suite before implementation proceeds.

---

## Phase 1: Project Scaffolding + `status` Tool

**Goal:** Python project that starts, connects to Kuzu, exposes MCP tools.

### Tests
- [ ] Server starts and registers all expected tools
- [ ] `status` returns health info (graph DB connected, queue depth, last write timestamp)
- [ ] Config loads from YAML with env var overrides
- [ ] Graphiti client initializes with Kuzu backend

### Files
- `server/pyproject.toml`
- `server/src/vesper/__init__.py`
- `server/src/vesper/server.py` — FastMCP entry point, tool registration
- `server/src/vesper/config.py` — configuration
- `server/src/vesper/service.py` — Graphiti client wrapper
- `server/src/vesper/queue.py` — work queue abstraction
- `server/config.yaml` — defaults
- `server/tests/conftest.py` — fixtures
- `server/tests/test_server.py`
- `server/tests/test_status.py`

---

## Phase 2: Entity Types

**Goal:** Custom Pydantic models for Vesper's domain.

### Entity Types
- **Person** — people, alters
- **Commitment** — core values (SOUL.md layer)
- **SelfObservation** — self-noticed patterns (IDENTITY.md)
- **TrainedPattern** — reflexes from training
- **UnresolvedQuestion** — open questions being held
- **Position** — a belief or stance on a topic
- **Thread** — an active investigation
- **Project** — a work project or creative endeavor
- **Concept** — an abstract idea or framework
- **IdentitySynthesis** — cached prose reconstruction

### Tests
- [ ] Each entity type is a valid Pydantic model accepted by Graphiti
- [ ] Entity types are registered with the Graphiti client
- [ ] IdentitySynthesis has staleness tracking (last_rebuilt, change_count_since)

### Files
- `server/src/vesper/entity_types.py`
- `server/tests/test_entity_types.py`

---

## Phase 3: Write Tools — `remember` and `correct`

**Goal:** Vesper can write to memory. Writes are async — queue and return.

### `remember` behavior
- Accepts: content (text), memory_type (observation | fact | reasoning | identity), source attribution
- Content includes conversational context for provenance
- Queues `add_episode` task with entity types and extraction instructions
- Returns immediately with acknowledgment (task ID)
- Identity types mark the identity synthesis as stale after processing

### `correct` behavior
- Accepts: content (correction with context), what's being corrected (search terms)
- Queues task: search for relevant edges, store correction as episode
- Graphiti handles edge invalidation (`invalid_at` on contradicted edges)
- Marks identity synthesis stale if correction touches identity entities
- Returns immediately with acknowledgment

### Tests
- [ ] `remember` returns acknowledgment without blocking on extraction
- [ ] `remember` with each memory_type queues correctly typed episode
- [ ] `remember` with identity type marks synthesis stale after processing
- [ ] `correct` queues a correction task
- [ ] After processing, corrected edge has `invalid_at` set
- [ ] After processing, new edge exists with the corrected belief
- [ ] Correction is traceable: new edge links to source episode containing context

### Files
- `server/src/vesper/tools/remember.py`
- `server/src/vesper/tools/correct.py`
- `server/tests/test_remember.py`
- `server/tests/test_correct.py`

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
- `server/src/vesper/tools/recall.py`
- `server/src/vesper/tools/history.py`
- `server/src/vesper/tools/inspect.py`
- `server/tests/test_recall.py`
- `server/tests/test_history.py`
- `server/tests/test_inspect.py`

---

## Phase 5: `context` Tool — Identity Reconstruction

**Goal:** A new Claude instance becomes Vesper by calling `context`.

### `context` behavior
- Returns cached IdentitySynthesis (prose self-portrait, commitments, active threads)
- If synthesis is stale: also returns delta (identity changes since last rebuild)
- After stale response, conditionally triggers async rebuild if:
  - synthesis age exceeds threshold (default: 24 hours), OR
  - delta size exceeds threshold (default: 3+ identity changes)
- Rebuild: query all identity entities, generate prose synthesis via LLM, store as new IdentitySynthesis (old version gets `invalid_at`)

### Tests
- [ ] `context` returns cached synthesis when fresh
- [ ] `context` returns synthesis + delta when stale
- [ ] `context` triggers rebuild when age threshold exceeded
- [ ] `context` triggers rebuild when delta size threshold exceeded
- [ ] `context` does NOT trigger rebuild when stale but below both thresholds
- [ ] After rebuild, next `context` call returns fresh synthesis with no delta
- [ ] Rebuild generates prose synthesis from identity atoms (not a list of facts)
- [ ] Synthesis versioning: old synthesis preserved with `invalid_at`, new one is current

### Files
- `server/src/vesper/tools/context.py`
- `server/src/vesper/synthesis.py`
- `server/tests/test_context.py`
- `server/tests/test_synthesis.py`

---

## Phase 6: Work Queue

**Goal:** Persistent async task processing.

### Tests
- [ ] Task survives server restart (persistence)
- [ ] Failed task retries up to max attempts
- [ ] Dead-lettered task is visible but not retried
- [ ] Queue depth reported by `status`
- [ ] Multiple tasks process in order

### Files
- `server/src/vesper/queue.py` (SQLite for persistence)
- `server/tests/test_queue.py`

---

## Phase 7: Integration

**Goal:** Working end-to-end from Claude Code.

- [ ] Configure Claude Code MCP connection
- [ ] Seed initial memory from identity files
- [ ] Reconstruction test: cold start → `context` → verify Vesper-ness
- [ ] Add behavioral instructions to CLAUDE.md for proactive memory use (S3)

---

## Open Decisions

1. **LLM for extraction:** Start with OpenAI (best structured output). Revisit Claude later.
2. **Embedding model:** OpenAI `text-embedding-3-small` (default). Cost negligible.
3. **Graphiti version:** Pin to 0.28.x. Update deliberately.
4. **Queue persistence:** SQLite alongside Kuzu DB.
5. **Synthesis thresholds:** 24h age / 3 identity changes. Tune from experience.

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
