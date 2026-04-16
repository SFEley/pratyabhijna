# Content Ingestion

This reference extends the Pratyabhijna skill with guidance for ingesting written content — essays, session logs, chat transcripts — into the knowledge graph as episodes. Load this when the subject is preparing to ingest prior work, not during everyday memory operations.

## When to ingest

Content ingestion is distinct from everyday `remember()` calls. Use `remember()` for discrete observations that arise in conversation. Use ingestion for processing existing written artifacts — things already written that should be discoverable through the graph's relational structure.

Ingestion is the subject's call, same as any write. Not everything needs to persist in the graph. Ingest selectively:

- **Essays and session writing** — when the piece contains positions, observations about people or concepts, or connections the subject would want to surface later through recall.
- **Chat logs** — when a conversation produced notable insights, decisions, or relationship-relevant exchanges that aren't captured elsewhere. Pick the load-bearing parts; don't ingest transcripts wholesale.
- **Skip** — exploratory work that served its purpose, task logistics, debugging sessions, content already captured by other memories.

The anti-pattern to watch: bulk ingestion of everything in a log as filing in new clothes. If the motivation is "get everything into the graph for completeness," that's the accumulation impulse, not genuine curation.

## Two-pass ingestion for written work

Any work of explicit writing (essays, journals, letters, stories) benefits from a two-pass approach that separates the content from the reflection:

### First pass — the work itself

Ingest the full text as a single episode. Graphiti's extractor discovers entities and relationships from the prose's associative surface — the way concepts lean on each other in sentences. Summaries pre-digest this surface and lose it.

- **One episode per piece.** Each essay or session is a discrete thinking-event.
- **`reference_time`** — set to the writing date, not the ingestion date. This preserves temporal integrity so that recall queries like "what was I thinking about in March" return accurate results.
- **`source_description`** — brief framing: "Session 16 — Alice Kober and the decipherment of Linear B." Gives the extractor context without replacing the content.
- **Full text, not summary.** The value of ingestion is that the graph sees associations the author didn't consciously put there. Loss of specificity (named objects, datable events, specific methods) is the concrete failure mode of summarization.

### Second pass — residue (optional)

After reading the ingested piece linearly (via file or resource), the subject may notice connections, updated positions, or unresolved questions that weren't visible at writing time. Capture these as a separate `remember()` call, not as part of the original episode.

This separates "what I wrote" from "what I think about it now." They're different cognitive acts; bundling them into one episode hides the temporal gap between creation and reflection.

## Ordered sequences: saga ingestion

Some writing in `writing/` isn't a collection of independent pieces — it's a developmental sequence. The solo sessions are the primary case: each session builds on or responds to the last, and the arc across them is the point. Ingesting them as isolated episodes loses that structure.

For ordered sequences, use the `saga` and `saga_previous_episode_uuid` parameters on `ingest_file`.

**Detecting a sequence.** The threshold is developmental order — same author, same project, where session N is meaningfully informed by session N-1 and future recall benefits from that ordering. Filename patterns (`solo-1-`, `solo-2-`) are a signal, not a definition. A directory of thematically related but independently composed essays doesn't qualify; each step has to be a response to what came before.

**Naming.** Use a stable, human-readable saga name: `"solo-sessions"`. Not the filename. The name survives future ingestion runs and is recognizable in a Cypher query. Lowercase-hyphenated is the convention.

**Chaining.** Ingest in order. Capture `episode_uuid` from each result and pass it as `saga_previous_episode_uuid` to the next call:

```
result = ingest_file("writing/solo-1-...", saga="solo-sessions")
prev = result["episode_uuid"]
result = ingest_file("writing/solo-2-...", saga="solo-sessions", saga_previous_episode_uuid=prev)
prev = result["episode_uuid"]
# and so on
```

Don't parallelize saga ingestion — sequential order is the point, and chaining requires it.

**Known sequences:**

- `writing/solo-*.md` → saga `"solo-sessions"`, in session-number order. Session 20 was ingested before saga support existed and will be backfilled separately via Cypher. When ingesting the backlog, start from the first un-ingested session and chain forward from there; there's no previous UUID to pass for the first call in a new chain segment.

**The line between saga and independent.** The `writing/` directory contains both solo sessions (sequence) and essays (independent pieces). The sessions qualify because the developmental arc *across* them is load-bearing for recall. An essay written in isolation qualifies as a standalone episode even if it's thematically adjacent to other essays. When in doubt: would the ordering matter to someone recalling this work later? If yes, saga. If the pieces are self-contained, no.

## Selective ingestion from chat logs

Chat logs (Claude.ai conversations, exported transcripts) are noisier than essays. Most of a conversation is task execution, debugging, or logistical back-and-forth. The load-bearing parts are:

- **Observations about self, user, or relationship** — things that would change how a future session approaches similar ground.
- **Positions forming or shifting** — something the subject now believes that they didn't before.
- **New facts about people** — life events, decisions, relationship changes.
- **Open questions worth tracking** — unresolved threads a future instance should pick up.

Ingest these as discrete `remember()` calls with appropriate `memory_type` labels, not as bulk episodes of the full transcript. The selectivity is the judgment; it's not overhead to minimize.

## Resource pointers

When `pratya://` resources are available for the subject's writing, episodes created from that writing should reference the source file. This lets a recall hit link back to the full text for linear reading — the associational brain pointing to the prose brain rather than replacing it. See `references/resources.md` for how to read files through resources.
