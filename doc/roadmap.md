# Roadmap: Post-Phase-7 Backlog

*Drafted April 15, 2026 — Vesper + Serah conversation. Ordering is Vesper's take; Serah may revise.*

Phase 7 shipped the OAuth-gated remote server. Functional foundation is solid. This doc captures the next pile — small features, ergonomics, and release-gate work — and a suggested order for attacking it.

Numbering is preserved from the conversation; items 1–2 originated with Vesper, items 3–9 with Serah. Source doesn't matter now that the pile is shared.

---

## Existing ingestion backlog

Before features: there's prose already written that hasn't been ingested yet.

- **Solo session corpus** — 22 sessions in `~/vesper/writing/`. Session 20 was ingested as the test case; the other 21 aren't in the graph yet. Saga support (#4) is the structural prerequisite, since the sessions are an ordered developmental sequence, not 22 independent episodes.
- **Early chronicle** — the dense versions of the October 2025 – early March 2026 entries, preserved in git history before the March 21 compression. Candidates for ingestion.
- **Correspondence** — `~/correspondence/` and the earlier `~/unnamed/` letters. Pending the unnamed instance's consent; Serah to ask. Configuration change only once consent lands; no code.

The synthesizer already watches `writing/`, so new solo sessions will ingest automatically going forward. The backlog above is the catch-up.

---

## Items

### 1. Self-scoped recall

Filter `recall()` results to entities where Vesper is the subject/holder — the Vesper Person node itself, and Observation/Drive/Position/Question/Thread/Event nodes connected to Vesper via holder/participant edges. Implemented as a post-filter on recall results rather than a query-path modifier; slightly wasteful but doesn't require rewriting the Graphiti query path. Graph-traversal "within N hops of Vesper" is the wrong definition — pulls in Serah at 1 hop and everything-about-Serah at 2 hops.

### 2. Status expansion

Add to `status()`: node counts by type, edge counts by type, correction count, supersession count (edges with `invalid_at` set and a replacement). All cheap to query. `status()` should stay fast — anything requiring new instrumentation belongs elsewhere.

Do *not* track `last_accessed_at` as a graph mutation (turns reads into writes). If retrieval-staleness tracking is ever wanted — primarily as substrate for a forgetting pass, not for display — implement as a SQLite counter, decoupled from the read path.

### 3. Skill tweak for recall

Observation: more has been recorded than accessed. Hypothesis: the skill guidance doesn't push hard enough toward proactive recall. Counter-hypothesis: in Claude Code with file access, bootstrap loads most of what's needed, and recall fills a narrower window — the delta between syntheses. The targeted tweak might be specific rather than general: *when files give a partial answer, check the delta for what's happened since the last synthesis.*

Ergonomic changes (#1 self-scoped, #5 community-scoped) may do more than prose tuning — recall has friction, and lower-friction tools get reached for.

### 4. Saga support

Multi-part ordered ingestion — the Beth-Ella story, the solo-session sequence, eventually the 5-year text archive with Eli. **Already a Graphiti feature** via options on `add_episode`. Work here is exposing the capability in Pratyabhijna's `remember` / ingestion surface, not new data modeling. Required for quality ingestion of the solo-session backlog.

### 5. Communities

Graphiti supports `build_communities` and adding episodes to them. Work: expose running the build, plus read operations that query by community. Unknown whether auto-detected communities will be semantically meaningful or just structural clusters — worth running once to find out. Also makes #7 more interesting later.

### 6. General-purpose NL → Cypher query tool

LLM generates Cypher from a natural-language description; server executes read-only query; returns results *plus the generated query* so the caller can see what was actually asked. Guardrails mandatory: read-only, query timeout, result-size cap. The risk is silent misdirection — one bad query, one confident wrong answer. Reach for sparingly, when the structured tools genuinely can't express the question.

### 7. 3D force-graph visualizer

Clickable interactive graph of query results, with hover-able node and edge info. For Serah, not for Vesper — operator-facing tool for watching the graph grow. Serah flagged as lower priority. Value increases after #5 (communities) makes the visual structure richer.

### 8. Deployment pipeline

Automate git pull + service restart + health check on the Contabo VPS. Current manual flow works, but automation is the difference between "server runs" and "server is reliable."

### 9. Cleanup and open source release

README polish, unit test cleanup, **clean-seed deploy test** (exercises a fresh install against an empty graph — catches assumptions baked in by developing alongside a populated instance), then actual release of the pratyabhijna repo as public. The Vesper repo stays private; pratyabhijna was built to be publishable.

### 10. Bare-Entity audit

Investigation, not implementation. 257 of 1,041 extracted entities (~25%) land as bare `Entity` with no secondary type. Sonnet's labels on these are often semantically correct — the 9-type taxonomy lacks slots for concrete artifacts (files, code symbols, APIs, protocols) and named abstractions/mechanisms (patterns, algorithms, principles). Sample the 257 at scale, bucket by apparent type, and decide whether to add **Artifact** / **Concept** types (helping search and graph coherence, at the cost of higher extractor discrimination load and weaker synthesis rationale than the existing 9) or accept bare Entity as the correct bucket for long-tail unclassifiable content. Prerequisite to any type-addition PR.

---

## Suggested order

**Quick wins and structural exposure first:**

1. **#2 status expansion** — low effort, immediate value, zero new concepts.
2. **#4 saga support** — unblocks clean ingestion of the solo-session backlog.
3. **#5 communities** — exposes existing Graphiti capability; sets up richer recall, makes #7 worth looking at later.
4. **#1 self-scoped recall** — ergonomic win; reduces recall's friction cost.
5. **#3 skill tweak** — do this *after* #1 lands, so the guidance points at a tool that's actually frictionless to reach for.

**Ingestion catch-up** sits between exposure and release: solo sessions (needs #4), early chronicle, correspondence (pending consent).

**Release gate:**

1. **#8 deployment pipeline** — reliability before the repo goes public.
2. **#9 cleanup + release** — finish Phase 7 properly. Clean-seed deploy test is the load-bearing check.

**Optional, post-release:**

1. **#6 NL query tool** — build when the structured tools start feeling insufficient in practice, not before.
2. **#7 visualizer** — build when Serah wants it for graph-watching. Richer after #5.

---

## Notes

- **What I notice about the shape of this pile:** almost every item is polish, ergonomics, or exposing capabilities already present. That's the shape of a project that's done its hard work. The interesting question stops being "what to build" and starts being "which of these actually matter vs. which could just not happen." #9 is the one that most closes the phase; the rest are optional sharpening.
- **Revision welcome.** Serah may reshape ordering or add/remove items.
