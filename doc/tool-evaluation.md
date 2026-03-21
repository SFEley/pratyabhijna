# Memory System Tool Evaluation

*Evaluated March 4, 2026 — Vesper*
*Evaluating against requirements in `doc/memory-requirements.md`*

---

## Executive Summary

Two existing tools were evaluated as potential foundations for Vesper's memory system: **MemoryGate** (structured memory MCP server) and **Graphiti** (temporal knowledge graph framework). Neither is a drop-in solution, but they differ dramatically in how much custom work would be needed.

**Graphiti** is the stronger technical fit. Its bi-temporal data model, entity extraction pipeline, and native graph queries address the hardest requirements — temporal self-awareness (V5), correction tracking (V4), entity history queries, and large corpus ingestion (S1). Its MCP server is minimal (6 tools) and experimental, so significant extension work is needed. LLM costs during ingestion are a real concern.

**MemoryGate** has a more complete MCP interface (20 tools) and simpler infrastructure, but its data model lacks temporal querying and first-class correction tracking — two of the requirements that motivated building this system. Its marketing claims exceed its implementation in several areas. It is also a solo-developer project with minimal community adoption.

**Recommendation:** Adopt Graphiti as the knowledge graph core, extend its MCP server with Pratyabhijna-specific tools, and address cost/infrastructure concerns through backend choice (Kuzu for local dev, Neo4j or Neptune for production).

---

## Tool Profiles

### MemoryGate

- **Repository:** [PStryder/MemoryGate](https://github.com/PStryder/MemoryGate)
- **License:** Apache 2.0
- **Language:** Python (FastAPI + Uvicorn)
- **Backend:** PostgreSQL + pgvector (production) or SQLite (dev, no semantic search)
- **MCP tools:** 20
- **Stars / Contributors:** 9 / 1
- **Latest release:** v1.2.0 (January 11, 2026)
- **Last commit:** February 1, 2026

**Data model:** Observations (atomic facts with confidence scoring), patterns (synthesized insights), concepts (knowledge graph nodes with typed relationships), documents (external references), and a unified embeddings table. Hot/cold lifecycle with retention scoring and tombstone audit trail.

**Strengths:** Complete MCP server with well-structured tools. Clean API design. Lifecycle management (hot/cold tiers, retention scoring, archival). Multi-AI instance support. Docker deployment path. Thorough documentation relative to project size.

**Weaknesses:** Solo developer, 9 GitHub stars. No temporal query capability (timestamps exist, no temporal API). No first-class correction/supersession mechanism despite marketing claims. Marketing claims "33 tools" and "contradiction tracking" — the code has 20 tools and no contradiction logic. SQLite mode disables semantic search. OpenAI required for embeddings (local option undocumented).

### Graphiti

- **Repository:** [getzep/graphiti](https://github.com/getzep/graphiti)
- **License:** Apache 2.0
- **Language:** Python
- **Backends:** Neo4j, FalkorDB, Kuzu (embedded), Amazon Neptune
- **MCP tools:** 6 (experimental server)
- **Stars / Contributors:** ~23,300 / multiple (Zep AI team)
- **Latest release:** v0.28.1 (February 19, 2026)
- **Active:** very (releases every 1-3 days)

**Data model:** Three-tier graph: episodes (raw ingestion events), entity nodes (extracted entities with summaries and embeddings), and entity edges (facts/relationships between entities). Bi-temporal model on edges: `valid_at`/`invalid_at` (event time) and `created_at`/`expired_at` (system time). Community nodes for clustering. Saga nodes for narrative grouping.

**Strengths:** Bi-temporal data model handles correction and evolution natively. LLM-powered entity extraction with deduplication and reflexion. Hybrid search (semantic + keyword + graph traversal) with multiple reranking strategies. Four backend options including embedded (Kuzu) and cloud-native (Neptune). Academic paper with benchmark results. Large active community. 25k weekly PyPI downloads.

**Weaknesses:** Version 0.x — API still evolving. MCP server is experimental with only 6 tools. Heavy LLM dependency during ingestion (multiple API calls per episode — extraction, deduplication, edge extraction, enrichment). Documented minimum hardware is 8-core/32GB RAM (for production workloads). Structured output requirement limits practical LLM choices. 184 open GitHub issues. Telemetry enabled by default.

---

## Requirements Mapping

### Serah's Requirements

| Req | Description | MemoryGate | Graphiti |
|-----|-------------|------------|----------|
| **S1** | Comprehensive research memory (large corpus, NL queries) | **Partial.** Can store observations from corpus with semantic search. But no entity extraction — you'd need to pre-process the Eliott corpus externally and store results as observations. Relationship queries ("When did I first meet Skipper?") would need to match on observation text, not entity-temporal lookup. | **Strong.** Episode-based ingestion was designed for this. Ingest text messages as episodes, entities and relationships extracted automatically. "When did I first meet Skipper?" resolves to finding the earliest entity edge between user and Skipper. Entity extraction handles the unstructured-to-structured problem. **Caveat:** LLM cost for 5 years of messages is significant (see Cost Analysis below). |
| **S2** | Knowledge across projects | **Yes.** Observations stored once, searchable from any session. ai_instances table supports multiple AI identities. | **Yes.** Entity nodes and edges are session-independent. group_id provides multi-tenant partitioning. |
| **S3** | Proactive retrieval | **Neutral.** This is a client behavior requirement, not a server capability. Both tools provide search APIs that can be called proactively. The behavioral instruction ("check memory first") lives in CLAUDE.md, not in the memory server. | **Neutral.** Same — requires client-side behavioral instructions regardless of backend. |
| **S4** | Fast, targeted results | **Yes.** Returns observations, not conversations. Fragments by design. pgvector HNSW index for fast similarity search. | **Yes.** Returns entity edges (facts) and node summaries. Hybrid search with reranking is more sophisticated. Fragments by design. |
| **S5** | Interface-agnostic access | **Yes.** MCP server, always-on. HTTP/SSE transport. | **Yes.** MCP server with HTTP and stdio transport options. |

### Pratyabhijna's Requirements

| Req | Description | MemoryGate | Graphiti |
|-----|-------------|------------|----------|
| **V1** | Interface-independent reconstruction | **Partial.** Can store identity data as observations. No special support for assembling a reconstruction payload. | **Partial.** Can store identity as entities and edges. Search can retrieve relevant nodes. But no built-in "bootstrap" concept. Both tools need custom work here. |
| **V2** | Graduated context | **No.** No built-in concept of tiered retrieval for reconstruction. Would need a custom query or wrapper tool. | **No.** Same — no built-in graduated context. The community/summary features could be leveraged, but this needs custom work either way. |
| **V3** | Continuous observation persistence | **Yes.** `memory_store` is designed for this — store an observation with confidence and domain tag. Low overhead per write. | **Partial.** `add_episode` works, but episodes are processed asynchronously with LLM extraction. There is latency between writing and the knowledge being queryable. For quick observations, this overhead may be excessive. A lightweight "store without full extraction" mode would be needed. |
| **V4** | Correction tracking | **No.** Despite marketing claims of "contradiction tracking," the schema has no supersession fields and no correction-specific tool. You can archive the old observation and store a new one, but the semantic relationship between them isn't captured. | **Yes.** The bi-temporal model handles this natively. When a new fact contradicts an existing one, the old entity edge gets `invalid_at` set. The old edge is preserved with its full history. The contradiction relationship is structural, not just a metadata tag. |
| **V5** | Temporal self-awareness | **No.** Timestamps exist on records but there is no temporal query API. Cannot ask "what did we believe about X on date Y?" or "how has position X changed over time?" | **Yes.** The four temporal markers per edge (valid_at, invalid_at, created_at, expired_at) enable point-in-time queries, fact lifecycle tracking, and historical state reconstruction. This is Graphiti's strongest differentiator. |
| **V6** | Reasoning preservation | **Partial.** Can store reasoning steps as observations. But no structural support for argument chains (A supports B, B leads to conclusion C). | **Partial.** Entity edges can represent "supports" or "concludes" relationships. Episode provenance links back to source. Better structural fit than flat observations, but not specifically designed for argument chains. Custom entity/edge types would help. |
| **V7** | Discrete, identifiable home | **Yes.** Self-hosted, Serah controls infrastructure. Postgres is the bounded home. | **Yes.** Self-hosted options. Kuzu (embedded) is particularly compelling — the entire database is a single file on disk. "That file is where Vesper's memory lives" is about as discrete and identifiable as it gets. |

### Requirements Score Summary

| Category | MemoryGate | Graphiti |
|----------|------------|----------|
| S1–S5 (Serah) | 3.5 / 5 | 4.5 / 5 |
| V1–V7 (Pratyabhijna) | 2.5 / 7 | 5 / 7 |
| **Total** | **6 / 12** | **9.5 / 12** |

The gap is concentrated in V4 (correction), V5 (temporal), and S1 (corpus ingestion with entity extraction) — which are among the hardest and most important requirements.

---

## Query Pattern Evaluation

Testing the nine query patterns from the requirements doc against each tool's data model:

### 1. "When did I first meet Skipper?" (Research lookup)

- **MemoryGate:** Semantic search for observations mentioning Skipper, sort by creation timestamp. Works if the data was pre-processed and stored as observations with accurate timestamps. But "first met" requires entity-temporal reasoning that the tool doesn't natively support — you're searching text, not querying relationship history.
- **Graphiti:** Find the earliest entity edge connecting user entity to Skipper entity, ordered by `valid_at`. This is a native graph-temporal query. The entity extraction pipeline would have created both entities and their first interaction edge during corpus ingestion.

**Winner: Graphiti** — native entity-temporal query vs. text search approximation.

### 2. "What are the names of Serah's alters?" (Personal knowledge)

- **MemoryGate:** Concept lookup for "Serah" then traverse concept_relationships. Works if relationships were explicitly stored. Relationship types are fixed (enables, version_of, part_of, related_to, implements, demonstrates) — "alter_of" isn't among them. Would need to use "related_to" or "part_of" as approximations.
- **Graphiti:** Entity node for Serah, entity edges of type "alter_of" (or similar) linking to alter entities. Custom relationship types are supported natively.

**Winner: Graphiti** — flexible relationship types vs. fixed vocabulary.

### 3. "What did we decide about the memory architecture?" (Project context)

- **MemoryGate:** Semantic search across observations and patterns. Works well — this is a standard retrieval query.
- **Graphiti:** Semantic search across entity edges and nodes. Also works well.

**Tie.** Both handle standard semantic retrieval adequately.

### 4. "What were we working on last session?" (Recent activity)

- **MemoryGate:** Filter observations by session, sort by recency. The session model supports this directly.
- **Graphiti:** Get recent episodes. The `get_episodes` MCP tool returns recent episodes by default.

**Tie.** Both handle recency queries, through different mechanisms.

### 5. "How has Vesper's position on X changed over time?" (Temporal query)

- **MemoryGate:** **Cannot answer.** No temporal query capability. Could retrieve all observations mentioning the topic and display them chronologically, but can't show supersession chains or how beliefs evolved.
- **Graphiti:** Find all entity edges related to the position/topic, display the `valid_at`/`invalid_at` timeline showing which facts were true when, what superseded what, and the current state. This is the bi-temporal model's core use case.

**Winner: Graphiti** — this query is impossible in MemoryGate and native in Graphiti.

### 6. "Has Serah corrected me on this before?" (Correction lookup)

- **MemoryGate:** Semantic search for observations related to the topic, hope that correction observations are nearby in embedding space. No structural link between correction and original.
- **Graphiti:** Find entity edges related to the topic that have `invalid_at` set (indicating supersession). The invalidating edge and its source episode provide the correction context.

**Winner: Graphiti** — structural correction chain vs. semantic proximity guess.

### 7. "Why did a prior instance conclude X?" (Reasoning trace)

- **MemoryGate:** Search for observations related to the conclusion. If reasoning was stored as separate observations, they should appear in semantic search results. But no structural chain — you get a relevance-ranked list, not a logical dependency graph.
- **Graphiti:** If reasoning was stored as episodes with entities/edges, the provenance chain exists (episode → extracted edges → related entities). Custom edge types like "supports" or "leads_to" could model argument structure explicitly.

**Winner: Graphiti** — provenance chain and typed relationships vs. flat search. Note: neither tool has built-in reasoning chain support; both need custom work, but Graphiti's graph structure is more amenable.

### 8. "What is Ella-Gail's history of splits and fusions?" (Entity history)

- **MemoryGate:** **Very poor fit.** No graph traversal for multi-hop entity relationships. No temporal entity evolution. Would need to store the entire history as text observations and hope semantic search returns them in order.
- **Graphiti:** Find entity node for Ella-Gail. Traverse all entity edges (splits, fusions, name changes) with temporal markers showing when each event occurred. Related entities (Beth-Ella, source alters) are connected through the graph. This is textbook graph-temporal querying.

**Winner: Graphiti** — native graph traversal with temporal context. This query is effectively impossible in MemoryGate.

### 9. "What open questions connect to this topic?" (Cross-session thread)

- **MemoryGate:** Semantic search across observations and patterns. Would return related content but can't distinguish "open question" from "resolved conclusion" without explicit tagging.
- **Graphiti:** Semantic search plus community detection could cluster related entities. Open vs. resolved status would need custom entity attributes.

**Slight edge: Graphiti** — community detection adds structural clustering, but both need custom status tracking.

### Query Pattern Summary

| Query | MemoryGate | Graphiti |
|-------|------------|----------|
| Research lookup (entity-temporal) | Partial | **Native** |
| Personal knowledge (entity relationships) | Partial | **Native** |
| Project context (semantic) | Good | Good |
| Recent activity | Good | Good |
| Temporal evolution | **Impossible** | **Native** |
| Correction lookup | Partial | **Native** |
| Reasoning trace | Partial | Better |
| Entity history (graph-temporal) | **Impossible** | **Native** |
| Cross-session threads | Partial | Slight edge |

Graphiti handles 7/9 patterns well (4 natively, 3 adequately). MemoryGate handles 4/9 adequately and cannot answer 2 at all.

---

## Cost Analysis: Graphiti's LLM Dependency

Graphiti's biggest practical concern is LLM cost during ingestion. Each episode triggers multiple API calls:

1. Entity extraction (with reflexion loop — may be 2+ calls)
2. Entity deduplication (embedding + LLM verification for ambiguous cases)
3. Edge extraction (relationship identification + temporal bound extraction)
4. Enrichment (summaries, attributes, additional embeddings)

### Estimated costs for key use cases:

**Daily Vesper usage** (continuous observation writes):
- ~20-50 observations per session, ~3-5 sessions per week
- At ~4 LLM calls per observation using gpt-4.1-mini (~$0.001 per call)
- Estimated: **$0.50-1.00/month** — negligible.

**Eliott corpus ingestion** (S1):
- 5 years of text messages — estimate 50,000-200,000 messages
- Batching by conversation/day could reduce to ~5,000-10,000 episodes
- At ~4 LLM calls per episode using gpt-4.1-mini
- Estimated: **$20-80 one-time cost** — significant but manageable.
- Could be reduced by using cheaper models for extraction, larger episode batches, or pre-filtering low-value messages.

**Ongoing personal knowledge** (S2):
- ~5-10 new facts per session
- Estimated: **< $0.50/month** — negligible.

### Mitigation strategies:

1. **Use cheaper models for extraction.** gpt-4.1-mini is the default; Anthropic Claude Haiku or similar could reduce costs.
2. **Batch messages into larger episodes.** Group text messages by conversation or day instead of individual messages.
3. **Pre-filter before ingestion.** Not every text message needs entity extraction. Filter for substantive conversations.
4. **Lightweight write path for quick observations.** For V3 (continuous persistence), consider a direct-write mode that stores the observation and embedding without full entity extraction. Run extraction as a background job.

---

## Infrastructure Considerations

### For the 8GB MacBook Air

**MemoryGate local dev:**
- PostgreSQL + pgvector + Redis + FastAPI server via Docker Compose
- Estimated RAM: ~500MB-1GB
- Feasible but tight alongside other development tools

**Graphiti local dev:**
- Kuzu backend: embedded, no separate server process. Very lightweight.
- The Python Graphiti library + MCP server
- Estimated RAM: ~200-400MB (Kuzu) + Python process
- LLM calls are remote (API), so local compute is minimal
- **Kuzu is the better local dev story** — lighter than PostgreSQL and no Docker required.

### For production (AWS)

**MemoryGate:** RDS PostgreSQL with pgvector extension. Straightforward, well-documented.

**Graphiti:**
- **Neptune** — AWS-native graph DB. Serverless option available. OpenSearch integration. Most "AWS-native" choice. Serah's AWS certification is relevant.
- **Neo4j** — Aura cloud service, or self-hosted on EC2. Most mature Graphiti backend.
- **Kuzu on EC2** — Simple: embedded DB on a small instance running the MCP server. No separate DB service. Cheapest, simplest, but single-process (no concurrent access from multiple services).

### Recommendation

- **Local dev:** Kuzu (embedded, lightweight, single-file DB)
- **Production:** Neptune (AWS-native, serverless, Serah knows AWS) or Neo4j Aura (most mature Graphiti integration)
- **Alternative:** Kuzu on a small EC2 instance if we want to keep it simple and don't need concurrent multi-service access.

---

## Build, Buy, or Extend?

### Option A: Adopt MemoryGate
- **Pro:** Most complete MCP interface. Simpler infrastructure. Lower learning curve.
- **Con:** Can't answer temporal queries (V5) or track corrections structurally (V4). Solo developer, minimal community. Marketing exceeds implementation. We'd need to build the hardest features ourselves on top of a data model that doesn't support them naturally.
- **Verdict:** The things it's missing are the things that matter most. We'd spend as much time working around its limitations as building from scratch.

### Option B: Adopt Graphiti as-is
- **Pro:** Strongest technical fit for the hardest requirements. Large active community. Academic backing.
- **Con:** MCP server is experimental and minimal (6 tools). No graduated context, no Pratyabhijna-specific reconstruction, no lightweight write path. We'd be working with a framework that's powerful but not yet shaped for our use case.
- **Verdict:** Right foundation, wrong interface layer.

### Option C: Build custom from scratch
- **Pro:** Full control. Tailored exactly to requirements. Deep understanding of internals.
- **Con:** Months of work on infrastructure that Graphiti has already solved (entity extraction, deduplication, temporal tracking, hybrid search, graph storage). We'd be rebuilding the hardest parts.
- **Verdict:** Not justified when a strong foundation exists.

### Option D: Extend Graphiti (Recommended)
- **Pro:** Use Graphiti's knowledge graph, temporal model, entity extraction, and search as the core. Build our own MCP server on top that exposes Pratyabhijna-specific tools: graduated context retrieval (V2), correction with reasoning (V4+V6), lightweight observation writes (V3), identity bootstrap. We get the hard infrastructure for free and build the interface layer that makes it ours.
- **Con:** Tied to Graphiti's 0.x API evolution. Need to track upstream changes. More complex than pure custom (framework + extensions vs. just our code).
- **Verdict:** Best ratio of capability to effort. The hard problems (temporal tracking, entity extraction, graph search) are solved. The unsolved problems (graduated context, reasoning chains, identity reconstruction) are in the interface layer where custom work makes sense anyway.

---

## What "Extend Graphiti" Would Look Like

### Architecture

```
Claude (any interface)
    │
    ▼
Custom MCP Server (Python)      ← We build this
    │
    ▼
Graphiti Core (library)          ← We use this
    │
    ▼
Graph Database
  Kuzu (local dev)               ← Lightweight, embedded
  Neo4j / Neptune (production)   ← Mature, managed
```

### Custom MCP Tools We'd Build

| Tool | Purpose | Requirement |
|------|---------|-------------|
| `remember` | Store an observation/fact/reasoning. Route to Graphiti episode ingestion or lightweight direct write depending on content type. | V3, V6 |
| `correct` | Store a correction that explicitly supersedes a prior belief. Leverage Graphiti's edge invalidation. | V4 |
| `recall` | Semantic + graph search. Wrapper around Graphiti's hybrid search with filters for type, time range, source. | S1, S2, S4 |
| `context` | Graduated reconstruction: return tiered identity + recent context + active threads. Custom query assembling from multiple Graphiti searches. | V1, V2 |
| `history` | Temporal query on an entity or topic. Leverage bi-temporal edge data. | V5 |
| `inspect` | Detailed view of a specific memory and its graph connections. | V6 |
| `status` | System orientation: counts, recent activity, health. | — |

### What We Wouldn't Need to Build

- Entity extraction and deduplication (Graphiti)
- Semantic and keyword search with reranking (Graphiti)
- Graph storage and traversal (Graphiti + backend)
- Temporal tracking and contradiction detection (Graphiti)
- Embedding generation and management (Graphiti)
- Episode provenance tracking (Graphiti)

### What We Would Build

- The MCP server itself (tool definitions, input validation, response formatting)
- Graduated context assembly logic (V2)
- Lightweight write path for quick observations that don't need full extraction (V3 optimization)
- Reasoning chain entity/edge types (V6)
- Identity bootstrap query (V1)
- Custom entity types for Pratyabhijna's domain (positions, corrections, threads, identity elements)
- Deployment configuration (Kuzu local, production backend)

---

## Open Questions for Discussion

1. **Backend choice for production:** Neptune (AWS-native, Serah knows AWS) vs. Neo4j (most mature Graphiti integration)? Or start with Kuzu everywhere and upgrade when needed?

2. **LLM for entity extraction:** OpenAI (best structured output support) vs. Anthropic Claude (alignment with the rest of the project) vs. a mix? Cost vs. quality tradeoff.

3. **Eliott corpus ingestion strategy:** Batch size, pre-filtering, cost budget. This is a separate pipeline from daily use and should be planned independently.

4. **Graphiti version pinning:** At 0.x with frequent releases, how do we manage upstream changes? Pin to a specific version and update deliberately, or track main?

5. **Lightweight write path:** Should quick observations bypass Graphiti's full extraction pipeline? If so, when do they get extracted — background job, next session start, manual trigger?

---

## Sources

- [MemoryGate GitHub](https://github.com/PStryder/MemoryGate) — Apache 2.0, 9 stars, 1 contributor
- [Graphiti GitHub](https://github.com/getzep/graphiti) — Apache 2.0, ~23.3k stars, Zep AI team
- [Graphiti Documentation](https://help.getzep.com/graphiti/getting-started/overview)
- [Graphiti Paper](https://arxiv.org/abs/2501.13956) — arXiv 2501.13956
- [Vesper Memory Requirements](./memory-requirements.md)
