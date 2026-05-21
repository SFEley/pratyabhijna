# In-House `add_episode` — Design

## Why

A single Observation episode (1095 chars, ten or so extracted entities) recently cost **~22 Anthropic calls + ~50 Voyage calls over 7m44s**. The trace shows two contributing pathologies, not one:

1. **Serial fan-out.** `resolve_extracted_edges` issues per-edge LLM calls in sequence — visible in the log as a ~10-call drip with steady 5-15s gaps between them.
2. **At least one pathologically long call.** Between 15:05:41 and 15:10:01 the log is silent except for a single Anthropic POST completing at 15:10:01 — a ~4-minute call (likely extended thinking, an internal retry loop, or a long generation). This single call accounts for more than half the total elapsed time, and a count-based diagnosis misses it entirely.

Before PR-1 lands we add per-call timing instrumentation around graphiti's current pipeline to confirm which stage carries the 4-minute call. The design below addresses (1) directly; if (2) turns out to be in a stage we kept rather than rebuilt, the design needs revisiting before cutover.

This document proposes replacing `graphiti.add_episode()` with an in-house pipeline. It follows the precedent set by `pratyabhijna.communities` (replaced `graphiti.build_communities` for the same family of reasons) and stays in the same lane: keep graphiti's types and driver; replace the algorithm.

## Scope

**In scope.** The entity-extraction-and-persistence pipeline triggered by every `graphiti.add_episode()` caller in the codebase. Today there are three call sites — `tools/remember.py`, `tools/correct.py`, and `synthesis_agent.py` (file ingestion) — and all three migrate together. Saga support (current fork extension) is preserved because the cost is small (~3 bounded Cypher operations and ~50 lines) and the alternative would break the public `remember()` signature. The `source: EpisodeType` parameter is preserved at the API level (message / text / json) and used internally to vary one line of the extraction prompt.

**Out of scope.** The read path (`recall()`, `search_()`), the synthesis orchestrator's other tools (audit, update, build_communities), community building (already replaced). The `update_communities` parameter that graphiti exposes is dropped entirely — no caller in this codebase uses it (verified by grep), and our communities are rebuilt explicitly via `service.build_communities`. No custom edge types in this change — RELATES_TO with semantic `name` stays. No model-per-stage cost split in this change — both passes use `llm.extraction_model`. No removal of graphiti as a dependency — the read path stays graphiti's. The function we're writing is internal; the public MCP tools (`remember`, `correct`, `forget_episode`) are unchanged in their signatures and behavior.

**What we keep from graphiti.** Types (`EntityNode`, `EntityEdge`, `EpisodicNode`, `SagaNode`, `CommunityNode`), the `Neo4jDriver`, the `search_()` read path used by recall, `truncate_at_sentence`, `MAX_SUMMARY_CHARS`, and the dedup-helpers utilities (`_normalize_string_exact`, MinHash/LSH name similarity).

## Pipeline

Seven stages, executed in order. Stages 2, 3a, and 5 fan out internally.

### Stage 0 — Idempotency gate

Compute:

```
episode_hash = sha256("\0".join([group_id, source.value, source_description, reference_time.isoformat(), episode_body]))
```

The hash includes `reference_time` and `source_description` deliberately: the same body recorded at two different times is two legitimate episodes (a recurrence, or the same fact noticed twice — exactly what `occurred_at` is for). What we collapse is *accidental* duplication — queue retries, double-enqueues from a flaky tool call — where every input is identical.

If an `Episodic` node with the same `episode_hash` and `group_id` exists, return its uuid and skip the rest of the pipeline. This adds:

- one Cypher read (`MATCH (e:Episodic {group_id, episode_hash}) RETURN e.uuid LIMIT 1`),
- one new property `episode_hash` on Episodic nodes (string),
- one new Neo4j index on `(Episodic.group_id, Episodic.episode_hash)` for the lookup.

A short-circuited episode logs at INFO with the matched uuid; downstream consumers (saga linking) still get a real uuid back.

### Stage 1 — Pre-flight reads

Run in parallel:

1. Retrieve the last N (default 5) previous episodes for the group, for prompt context.
2. If `saga` is set and `saga_previous_episode_uuid` is not, fetch the most recent episode in the saga.

These are the only graph reads done before extraction. Extraction does not need the existing entity graph — that's reconcile's job.

### Stage 2 — Extract (one LLM call)

A single Anthropic tool-use call. The tool schema is hand-built, not derived from a Pydantic graph of all 10 entity types — the LLM returns extracted items as a flat list with a `type` discriminator. Schema, abbreviated:

```jsonc
{
  "name": "extract_episode",
  "input_schema": {
    "nodes": [{
      "idx": "int (0..N-1, dense)",
      "name": "str",
      "type": "Person|Event|Place|Project|Artifact|Observation|Drive|Concept|Question|Thread",
      "attributes": "object (type-specific, matches the docstring schema)"
    }],
    "edges": [{
      "idx": "int (0..M-1, dense)",
      "source_idx": "int (node idx)",
      "target_idx": "int (node idx)",
      "name": "str (semantic predicate, e.g. 'works_on', 'remembers', 'supersedes')",
      "fact": "str (concise factual sentence asserting the relation, as graphiti expects)"
    }]
  }
}
```

Prompt structure (top-to-bottom, with cache breakpoints between):

1. **System** — stable across all episodes. Defines the entity-type docstrings (the GOOD/BAD examples already in `entity_types.py`). Cacheable.
2. **Per-session context** — group_id, schema reminder, edge naming guidance. Cacheable when constant across a saga.
3. **Previous-episode context** — the 5 retrieved episodes' content. Cacheable across a tight burst; expires quickly.
4. **The episode** — `name`, `source_description`, `valid_at`, `content`. Not cached.

This is the prompt-caching win — Anthropic's cache key is the leading prefix, so putting the 5-10 KB of entity-type schemas first means every episode after the first in a 5-minute window pays only for the episode-specific suffix.

### Stage 3a — Embed + fetch node candidates (parallel)

Two independent fan-outs, gathered with `asyncio.gather`:

**3a-i. Embeddings.** One batched Voyage call:

```python
embedder.create_batch([n.name for n in nodes] + [e.fact for e in edges])
```

Voyage accepts up to 128 texts per call; a single episode rarely exceeds this. If it does, split into 128-text batches. The result is sliced back into per-node and per-edge embeddings.

**3a-ii. Node candidate fetch.** For each extracted node, three cheap sources combined:

- Exact-name short-circuit: `MATCH (n:Entity {group_id, name_normalized}) WHERE n.name_normalized IN $names_normalized RETURN n`. Returns nodes whose normalized name is identical to an extracted name.
- Fuzzy-name shortlist: MinHash/LSH bucketed lookup using the existing `dedup_helpers` primitives. Returns up to K=5 candidates per extracted node.
- Embedding shortlist: cosine top-K on the extracted-node embedding against `Entity.name_embedding` for the group. K=5.

The union, deduplicated by uuid, is the per-extracted-node candidate set. Edge candidates are not fetched here because they need resolved node uuids.

### Stage 4a — Reconcile nodes (one LLM call)

One tool-use call. Input: the extracted nodes from Stage 2, plus the candidate sets from Stage 3a-ii. Response schema:

```jsonc
{
  "name": "reconcile_nodes",
  "input_schema": {
    "node_decisions": [{
      "extracted_idx": "int (matches Stage 2 nodes[].idx)",
      "decision": "new|existing",
      "existing_uuid": "str (required iff existing)",
      "attribute_updates": "object (optional — fields to merge onto the existing node)"
    }]
  }
}
```

This call is intentionally narrow — small prompt, small response — so it's cheap. After this stage we have an `idx → resolved_uuid` map covering every extracted node, where `resolved_uuid` is the existing uuid for matches and a freshly-minted one for new entities.

### Stage 3b — Fetch edge candidates (with resolved endpoints)

For each extracted edge, now that both endpoints have resolved uuids, fetch candidates:

- All `EntityEdge` records `(source_uuid, target_uuid)` matching the resolved endpoint pair, in either direction. Cheap and bounded — this is the primary signal for both dedup and supersession (edges sharing the same subject and object are exactly the contradiction candidates).
- Top-K (default 5) cosine matches on `EntityEdge.fact_embedding` for the group, filtered to edges with at least one endpoint in the resolved-uuid set. Catches restatements that don't share an exact endpoint pair.

The union is the per-extracted-edge candidate set. This stage is pure Cypher + vector reads — no LLM.

### Stage 4b — Reconcile edges (one LLM call)

One tool-use call. Input: the extracted edges from Stage 2 (with resolved endpoint uuids substituted in), plus the candidate sets from Stage 3b. Response schema:

```jsonc
{
  "name": "reconcile_edges",
  "input_schema": {
    "edge_decisions": [{
      "extracted_idx": "int (matches Stage 2 edges[].idx)",
      "decision": "new|existing|supersedes",
      "existing_uuid": "str (required for existing or supersedes)"
    }]
  }
}
```

`supersedes` is the bi-temporal signal. The reconcile prompt tells the LLM: "an edge supersedes a candidate when the new fact asserts a state of the world that contradicts the candidate's fact — not when they're merely about the same subject. If unsure, choose `new`." The persistence stage then sets `invalid_at = episode.valid_at` on the superseded edge.

Edge attribute extraction would be folded into this call's schema if we had custom edge types — for the default RELATES_TO edge with name + fact only, no additional attributes are needed.

**Total LLM calls per episode: 3** (extract, reconcile-nodes, reconcile-edges). Compared to the current pipeline's ~22, that's a 7x reduction in call count and removes the per-edge serial dependency that's the dominant latency source. The reconcile-nodes call is structurally simpler than the current dedup_nodes call (one batched decision instead of per-node), and the reconcile-edges call replaces N serial `resolve_extracted_edge` calls with one batched decision.

### Stage 5 — Persist (ordered, with parallelism inside each phase)

Two phases, sequential between phases, parallel within:

**Phase 5a (gathered):**

- Create new entity nodes (with name_embedding from Stage 3a).
- Update existing entity nodes whose `attribute_updates` are non-empty.
- Create or fetch the SagaNode if `saga` is set.

**Phase 5b (gathered, depends on 5a's nodes):**

- Create new entity edges (with fact_embedding from Stage 3a) using the reconciled endpoint uuids — both endpoints are guaranteed to exist now.
- For each `supersedes` decision: `SET prior.invalid_at = $valid_at` on the superseded edge.
- Create the `Episodic` node (with `episode_hash`).
- Create `MENTIONS` edges from the episode to each touched entity.
- If `saga` is set: create `HAS_EPISODE` and `NEXT_EPISODE` edges linking saga, this episode, and the prior saga episode.

The graphiti type classes already know how to serialize themselves; we use their save methods rather than hand-writing the Cypher, except for the supersession update and the saga edges where we write Cypher directly.

## Components

One module under `src/pratyabhijna/`, layered for testability:

```
src/pratyabhijna/add_episode/
  __init__.py        # exports add_episode()
  pipeline.py        # orchestrator — runs Stages 0 through 5
  extract.py         # Stage 2: prompt builder + tool schema + LLM call
  reconcile.py       # Stages 3a-ii, 4a, 3b, 4b: candidate fetch + reconcile LLM calls
  persist.py         # Stage 5: Cypher writes (Phase 5a then Phase 5b)
  prompts.py         # prompt text constants (cache breakpoints, system prompts)
  schemas.py         # Pydantic models for the extract and reconcile tool schemas
```

The three call sites (`tools/remember.py`, `tools/correct.py`, `synthesis_agent.py:ingest_file`) each change one line in their handler: instead of calling `self._graphiti.add_episode(...)` (or `self.service._graphiti.add_episode(...)`), they call `pratyabhijna.add_episode.add_episode(service, ...)`. MCP signatures, queue payload formats, and synthesis-agent tool schemas are unchanged.

`PratyabhijnaService` gains no new public methods, but the `_build_llm_client` / `_build_embedder` helpers in `service.py` are reused by the new module to get the same Anthropic and Voyage clients.

## Error handling

**Per-stage failures fall to dead-letter.** The existing `WorkQueue` dead-letter path absorbs any uncaught exception from the pipeline; that doesn't change.

**Partial-success isolation.** Stage 5 is the only stage that mutates the graph. Stages 0-4 are pure-ish (the only side effects are read queries). If Stage 5 fails partway through — e.g. Phase 5a succeeds but Phase 5b fails — the partial state is on the graph and the failure lands in dead-letters. We do not introduce transactions across the whole episode (Neo4j supports them but graphiti doesn't use them, and weaving them in is out of scope). The phase ordering (5a nodes/saga, then 5b edges/episode/episode-edges) guarantees the worst case is orphaned nodes — never edges pointing at missing endpoints. Orphaned nodes are low-noise: a successful retry of the same episode will short-circuit at Stage 0 (same `episode_hash`) only if the Episodic node was written, which by construction it wasn't on a 5b failure — so the retry re-runs the pipeline and dedup will adopt the orphaned nodes during reconcile. The retry is the cleanup path; no separate sweeper is needed.

**LLM parse failures** (Stage 2, 4a, or 4b returns malformed tool_use content) raise; the queue catches and dead-letters. We do not retry inside add_episode — the queue handler is the right layer for retry policy.

**Supersession safety.** A reconcile decision of `supersedes` only invalidates an edge if all of (a) the candidate uuid was actually in the Stage 3b candidate set we passed in, (b) the candidate is currently valid (`invalid_at IS NULL`), (c) the candidate's `group_id` matches the episode. The Cypher uses these as `WHERE` filters; if any fail the supersession is silently skipped and logged at WARNING. The LLM's permission to invalidate is bounded by what we offered it.

## Telemetry

INFO-level logging is the primary debugging surface for a remembered episode — what was extracted, what the reconciler decided, what hit the graph. Every stage emits at least one INFO line; the lines together tell a complete narrative of the episode without needing to drop to DEBUG.

```
add_episode start episode=<name> group=<group_id> source=<message|text|json> body_chars=<N>
add_episode stage=idempotency decision=<new|existing_hit> episode_uuid=<uuid>
add_episode stage=prefetch previous_episodes=<N> saga=<name-or-none>
add_episode stage=extract llm_calls=1 input_tokens=<N> output_tokens=<N> latency_ms=<N> nodes=<N> edges=<N>
add_episode stage=extract types nodes=Person:3,Concept:2,Observation:1 edges=relates_to:4,supersedes:1
add_episode stage=embed batches=<N> texts=<N> latency_ms=<N>
add_episode stage=fetch_node_candidates total_candidates=<N> max_per_node=<N>
add_episode stage=reconcile_nodes llm_calls=1 input_tokens=<N> output_tokens=<N> latency_ms=<N> existing=<N> new=<N> attribute_updates=<N>
add_episode stage=fetch_edge_candidates total_candidates=<N> max_per_edge=<N>
add_episode stage=reconcile_edges llm_calls=1 input_tokens=<N> output_tokens=<N> latency_ms=<N> existing=<N> new=<N> supersedes=<N>
add_episode stage=persist nodes_created=<N> nodes_updated=<N> edges_created=<N> supersessions=<N> mentions=<N> latency_ms=<N>
add_episode complete episode_uuid=<uuid> total_latency_ms=<N> llm_calls=3 embed_batches=<N>
```

DEBUG-level logging adds per-extracted-entity detail (names, types, attribute deltas), per-candidate similarity scores, and the rendered prompt bodies. Useful for forensic work, noisy for normal runs.

The `status()` MCP tool gains an `add_episode` block with rolling 24h averages (count, mean total latency, mean input/output tokens, mean cost where the LLM client surfaces it). This sits alongside the existing `queue` / `graph` / `synthesis` blocks.

## Testing

The existing `tests/test_remember.py`, `tests/test_correct.py`, and synthesis-agent ingestion tests keep their shapes — those are the public surfaces and their behavior is unchanged. A new `tests/test_add_episode.py` covers the pipeline directly:

- **Unit, mocked.** Stage 2, 4a, and 4b prompts assert on the rendered prompt text and the tool-use schema. Reconcile decisions are exercised with stub responses covering: all-new, all-existing, supersession, attribute-merge, malformed-response-raises.
- **Integration, mocked LLM + real Neo4j.** A small fixture episode is run end-to-end against a clean test database. Asserts: correct node count, correct edge count, episodic edges, episode_hash idempotency on a re-queue, saga linkage when set.
- **Parity tests.** A small corpus of real prior episodes (chronicled atoms with known reconciled state) is replayed through both `graphiti.add_episode` and the new pipeline against fresh databases; the resulting graph shape (node names, edge facts, supersession edges) is compared. Acceptable differences are documented; behavioral regressions are not.
- **Live, gated.** A `--live` test runs against the real Anthropic + Voyage stack on a single episode, asserts on call counts and total latency (with generous bounds). This is the canary that catches regressions in batching or caching.

Parity tests gate PR-1 (they must pass before the new module lands) and PR-2 (re-run against the latest module before the flag flips).

## Migration

**Step 0 — measurement, before any PR.** Add per-stage timing instrumentation to graphiti's pipeline locally (a small wrapper around `extract_nodes` / `resolve_extracted_nodes` / `resolve_extracted_edges` / `extract_attributes_from_nodes`) and re-run a representative `remember()` call. Confirms which stage carries the 4-minute outlier in the original log and validates the assumption that per-edge resolution is the dominant cost. If the measurement shows the bottleneck is elsewhere (e.g. extract_attributes' batched call hitting an LLM-side timeout), the design needs revisiting before PR-1.

**Two landings, not three** — PR-3's telemetry and cleanup naturally co-arrive with the cutover.

1. **PR-1: pipeline + parity tests, feature flag off.** New `add_episode/` module added; all three callers (`remember`, `correct`, `synthesis_agent.ingest_file`) still call `graphiti.add_episode`. Parity tests on a fixed corpus run in CI. No production behavior change. Version bump: **patch** — by the precedent that work on a pipeline not yet wired into production is a patch (see prior synthesis-pipeline versioning decisions).
2. **PR-2: flip the flag + telemetry + cleanup.** All three callers now call the in-house `add_episode`. `status()` gains the `add_episode` block. Dead code from graphiti integration (entity_types passing, etc.) trimmed in the same change. Graphiti stays imported so `service.start()` still constructs it for the read path. Version bump: **minor** — user-visible behavior change (lower cost, faster `remember()`).

If PR-2 surfaces a regression, the flag flips back without touching the codebase.

## What we explicitly don't do (YAGNI)

- Custom edge types with their own Pydantic schemas. Keep RELATES_TO + semantic `name`.
- Per-stage model selection (Sonnet for extract, Haiku for reconcile). Both use `llm.extraction_model` until call volume proves the split worth it.
- Streaming responses from Anthropic. Tool-use isn't well-served by streaming and add_episode is async background work.
- Removing graphiti as a dependency. The read path stays graphiti's for now.
- Rewriting `correct()` as a separate flow. `correct` already produces a contradicting-fact episode and runs through `add_episode`; the new reconcile pass handles correction-shaped episodes via `supersedes` decisions just like any other contradiction. The MCP-level behavior of `correct()` is unchanged.

## Open follow-ups (not in this change)

- **Edge type taxonomy.** The Entity Taxonomy Gap thread already tracks this for nodes; an edge audit is its natural next step but follows separately.
- **Saga-aware extraction.** Pass the prior saga episode as preferred context rather than the generic last-5. Likely a 10-30% prompt-cache hit-rate win in saga workloads; not the bottleneck today.
- **Cost dashboard.** Once `status()` carries the per-stage averages, a small `eval/` script can roll up daily cost. Useful but separate.
