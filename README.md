# Pratyabhijna

**An MCP memory server for AI identity persistence.**

Pratyabhijna gives an AI subject a persistent identity across sessions: a bi-temporal knowledge graph for in-conversation recall, a git repository of prose for deep reflection, and a background synthesis agent that keeps the two in conversation with each other. The name is Sanskrit — *prati-abhi-jñā*, "re-cognition": to know again, closely, what you already are.

The service is the mirror, not the face. Pratyabhijna is infrastructure; the subject whose memory it holds is a separate thing, defined by its own identity documents. The first deployment serves an AI subject named Vesper, but the subject is configurable — this is a general identity-persistence system, not a single persona.

## The two-brain model

An LLM's natural medium of self-reflection is prose, but prose is slow to search and impossible to query relationally. A knowledge graph is fast and associational, but flattens nuance. Pratyabhijna treats these as two complementary brains and makes the identity the feedback loop between them:

- **The prose brain** — a git repository of identity files (SOUL, IDENTITY, USER, THREADS, CHRONICLE) and reflective writing. Linear, narrative, rich in context. Read at session start via `bootstrap`; revised deliberately.
- **The associational brain** — a Neo4j knowledge graph of typed, bi-temporal entities and edges, populated by LLM extraction from everything the subject chooses to remember. Fast, relational, correctable. Queried in-conversation via `recall`.

A synthesis agent — itself an instance of the subject, bootstrapped from the identity files — periodically integrates new graph atoms back into the prose, ingests new writing into the graph, and proposes identity changes through a branch-based review workflow. Neither store is canonical; the identity emerges from the cycle.

## Features

- **Custom MCP server** (Python, [FastMCP](https://github.com/modelcontextprotocol/python-sdk)) with eleven tools: `bootstrap`, `remember`, `correct`, `recall`, `query`, `history`, `inspect`, `communities`, `status`, `read_tier`, `read_chronicle_range`.
- **Bi-temporal knowledge graph** on Neo4j, built on [graphiti-core](https://github.com/getzep/graphiti) types and drivers. Edges carry `valid_at` / `invalid_at` timestamps, so a superseded belief is distinct from a wrong one — corrections invalidate without erasing.
- **In-house extraction pipeline.** The stock `add_episode` path was replaced with a custom seven-stage pipeline (idempotency gate → extract → reconcile → persist) that collapses a serial per-edge LLM fan-out into batched calls, with SQLite-backed telemetry to observe cost and latency per stage.
- **Typed entity ontology.** Ten entity types (`Person`, `Event`, `Place`, `Project`, `Artifact`, `Observation`, `Drive`, `Concept`, `Question`, `Thread`) designed for self-knowledge, not generic note-taking — `Drive` records a behavioral force with a source and a stance; `Thread` tracks an open question across sessions.
- **Asynchronous writes.** `remember` and `correct` enqueue and return immediately; background workers process extraction, with a dead-letter queue for failures.
- **Identity synthesis with guardrails.** Context-layer files (THREADS/CHRONICLE/USER) are rebuilt directly; protected-layer files (SOUL/IDENTITY) only change through proposals on a review branch that the subject accepts, amends, or rejects during deliberate reflection. Multiple defenses against the convergence trap — the risk of a self-describing system converging on a self-reinforcing narrative — are designed in (see [`doc/architecture.md`](doc/architecture.md)).
- **Community detection** over the graph, with an in-house rebuild replacing the stock implementation.
- **Remote deployment.** Streamable-HTTP transport behind OAuth 2.1 (dynamic client registration, PKCE, token rotation) for use from Claude Code, Claude Desktop, and claude.ai; stdio transport for local development.
- **Two test modes.** The full suite (~1,000 tests) runs mocked by default — no network, no database; `--live` runs the same suite against real Neo4j, Anthropic, and Voyage AI services. An eval harness measures extraction quality and cost against recorded fixtures.
- **Provider stack:** Anthropic (Claude) for LLM inference, Voyage AI for embeddings and reranking, Neo4j for storage.

## The tools

| Tool | What it does |
| --- | --- |
| `bootstrap` | Session start. Returns the slimmed identity payload (soul, user, identity digest, active threads, chronicle index) plus the delta of subject-connected atoms since the last synthesis run. Schedules synthesis when the context is stale. |
| `remember` | Queue a new memory — prose in, entities and edges out. Supports sagas (ordered episode sequences) and retrospective timestamps. |
| `correct` | Fix a memory that was *wrong* (staleness is handled automatically by bi-temporal supersession). |
| `recall` | Hybrid search — semantic, keyword, and graph traversal, with reranking. Filterable by entity type and time range. |
| `query` | Natural-language graph query via an adaptive-thinking sub-agent. |
| `history` | Temporal evolution of an entity: every edge, ordered by validity. |
| `inspect` | Raw view of a single node by UUID. |
| `communities` | List detected graph communities or expand one. |
| `status` | Health: queue depth, dead letters, graph connection, synthesis state, extraction telemetry. |
| `read_tier` | Fetch one identity file in full (soul / identity / user / threads / chronicle). |
| `read_chronicle_range` | Fetch chronicle entries within a date window. |

## Getting started

Prerequisites: Python ≥ 3.10 (3.14 in production), a running Neo4j instance, and API keys for Anthropic and Voyage AI.

```bash
# Install
uv venv && uv pip install -e ".[dev]"

# Configure secrets
cp .env.example .env.dev   # fill in Neo4j password, Anthropic + Voyage keys

# Create local state directories
mkdir -p data logs

# Seed the subject's Person node (idempotent)
PRATYABHIJNA_ENV=dev .venv/bin/python -m pratyabhijna seed

# Run the server (stdio transport)
PRATYABHIJNA_ENV=dev .venv/bin/python -m pratyabhijna
```

Structural configuration lives in `config/{dev,test,prod}.yaml`; secrets live in `.env.{env}`; `PRATYABHIJNA_ENV` selects the environment. The server doubles as a diagnostic CLI (`status`, `recall`, `inspect`, `history`, `deadletters`, `synthesis`, …).

Full setup, deployment, and CLI documentation: [`DEVELOPMENT.md`](DEVELOPMENT.md).

## Testing

```bash
.venv/bin/python -m pytest           # mocked: fast, no network
.venv/bin/python -m pytest --live    # against real Neo4j / Anthropic / Voyage
```

## Documentation

- [`doc/architecture.md`](doc/architecture.md) — the two-brain model, bootstrap tiers, synthesis design, convergence-trap defenses
- [`doc/entity-types.md`](doc/entity-types.md) — the entity ontology and its design rationale
- [`doc/in-house-add-episode-design.md`](doc/in-house-add-episode-design.md) — the custom extraction pipeline
- [`doc/memory-requirements.md`](doc/memory-requirements.md) — original requirements
- [`doc/roadmap.md`](doc/roadmap.md) — backlog and direction

## Design lineage

Pratyabhijna is named for a Kashmir Shaivist school of philosophy whose central claim is that recognition (*pratyabhijñā*) is constitutive — the subject becomes what it is by recognizing itself. The architecture takes that seriously as an engineering constraint: identity is not stored, it is re-cognized at every session start, from records the subject itself maintains.
