# Pratyabhijna — Developer Guide

Pratyabhijna is a custom MCP memory server built on [graphiti-core](https://github.com/getzep/graphiti). It gives an AI subject persistent identity across sessions: a knowledge graph for in-session recall, a git repository of prose for deep reflection, and a synthesis agent that integrates the two. The service is the mirror, not the face.

For architecture, see [`doc/architecture.md`](doc/architecture.md). For the implementation history, see [`doc/implementation-plan.md`](doc/implementation-plan.md).

---

## Prerequisites

- **Python 3.13.** (3.14 has no Kuzu wheels; 3.13 is the target.)
- **Neo4j** running locally. The easiest option on macOS is [Neo4j Desktop](https://neo4j.com/download/). Dev config expects `neo4j://127.0.0.1:7687`.
- **API accounts:** Anthropic (for LLM) and Voyage AI (for embeddings/reranking).
- **A subject repo.** Pratyabhijna reads identity files from a configured git repo path (default: `~/vesper`). The repo should contain `memory/SOUL.md`, `memory/IDENTITY.md`, `memory/USER.md`, `memory/THREADS.md`, `memory/CHRONICLE.md`. See `doc/architecture.md` for what each file holds.

---

## Setup

### 1. Create a virtual environment

```bash
python3.13 -m venv .venv
```

Python's venv on macOS omits pip by default. Bootstrap it explicitly:

```bash
.venv/bin/python3.13 -m ensurepip --upgrade
```

### 2. Install the package and dev dependencies

```bash
.venv/bin/python3.13 -m pip install -e ".[dev]"
```

This installs `pratyabhijna` in editable mode plus `pytest` and `pytest-asyncio`.

### 3. Configure secrets

Copy the example env file and fill in real values:

```bash
cp .env.example .env.dev
```

Required keys in `.env.dev`:

```
PRATYABHIJNA_NEO4J__PASSWORD=<your Neo4j password>
PRATYABHIJNA_LLM__API_KEY=<your Anthropic API key>
PRATYABHIJNA_EMBEDDING__API_KEY=<your Voyage AI API key>
```

Leave `PRATYABHIJNA_SERVER__URL` and `PRATYABHIJNA_API_KEY` empty for local stdio development. They're only needed for the HTTP transport + OAuth deployment.

Structural config (non-secrets) lives in `config/dev.yaml`. Edit that file to change Neo4j URIs, model names, synthesis thresholds, and so on.

### 4. Create the `data/` directory

The queue and OAuth SQLite databases land here. It's gitignored.

```bash
mkdir -p data logs
```

### 5. Seed the subject node

The knowledge graph needs a Person node representing the subject before any tool can return identity data:

```bash
PRATYABHIJNA_ENV=dev .venv/bin/python3.13 -m pratyabhijna seed
```

This creates the node (or is a no-op if it already exists). Re-running is safe.

---

## Running the server

### Local development (stdio transport, Claude Code)

The repo includes `.mcp.json` at the root. Claude Code picks it up automatically when the project is open — no manual configuration needed.

To run manually:

```bash
PRATYABHIJNA_ENV=dev .venv/bin/python3.13 -m pratyabhijna
```

### Connecting from all Claude Code sessions (user-level)

To make the server available in every Claude Code session regardless of working directory, add it to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "Pratyabhijna": {
      "command": "/path/to/pratyabhijna/.venv/bin/python3.13",
      "args": ["-m", "pratyabhijna"],
      "cwd": "/path/to/pratyabhijna",
      "env": { "PRATYABHIJNA_ENV": "dev" }
    }
  }
}
```

### Production (streamable-http, OAuth)

Production runs on a VPS behind Caddy (auto-TLS). See `doc/architecture.md` for the full deployment description. The short version:

1. Set `PRATYABHIJNA_SERVER__URL` and `PRATYABHIJNA_API_KEY` in `.env.prod`.
2. Run with `PRATYABHIJNA_ENV=prod`.
3. Caddy proxies from 443 → 127.0.0.1:3000; DNS rebinding protection is disabled in config because the proxy forwards an external `Host` header.

---

## Running tests

### Mock mode (default)

All external dependencies (Neo4j, Anthropic, Voyage AI) are mocked. Runs fast, no network required.

```bash
.venv/bin/python3.13 -m pytest
```

### Live mode

Runs against real services. Requires Neo4j running and all API keys set in `.env.test`.

```bash
.venv/bin/python3.13 -m pytest --live
```

Live tests write to and read from the test Neo4j database. They clear the graph before running.

---

## CLI reference

The server doubles as a CLI for diagnostics. All commands require `PRATYABHIJNA_ENV` to be set (defaults to `dev`).

```bash
# System health
python -m pratyabhijna status

# Identity bootstrap (what the MCP tool returns)
python -m pratyabhijna bootstrap

# Search the graph
python -m pratyabhijna recall "some query" [--type Observation] [--time-range 7d]

# Node detail
python -m pratyabhijna inspect <uuid>

# Entity history
python -m pratyabhijna history "Vesper"

# Seed the subject node
python -m pratyabhijna seed [--name NAME]

# Dead-letter queue management
python -m pratyabhijna deadletters list
python -m pratyabhijna deadletters show <id>
python -m pratyabhijna deadletters retry <id>|--all
python -m pratyabhijna deadletters purge <id>|--all
```

---

## Project layout

```
config/
    dev.yaml          Structural config for dev environment
    test.yaml         Structural config for test environment
    prod.yaml         Structural config for production
.env.example          Template for secrets (copy to .env.{env})

src/pratyabhijna/
    __main__.py       CLI entry point and server startup
    server.py         FastMCP server: tool registration
    service.py        Graphiti client wrapper and lifecycle
    config.py         Pydantic-settings config model
    queue.py          Persistent async work queue (SQLite-backed)
    synthesis.py      Staleness checks, identity atom queries, repo scanning
    synthesis_agent.py  Synthesis agent loop, tool schemas and implementations
    entity_types.py   Custom Pydantic entity types for Graphiti extraction
    git_ops.py        Async git operations used by the synthesis agent
    log.py            Logging configuration
    reranker.py       Voyage AI cross-encoder reranker
    deadletters.py    Dead-letter queue CLI helpers
    resources.py      MCP resource registration (pratya:// URIs)
    seed.py           Subject node bootstrap

    tools/
        bootstrap.py  bootstrap MCP tool
        remember.py   remember MCP tool + add_episode queue handler
        correct.py    correct MCP tool + correct_memory queue handler
        recall.py     recall MCP tool
        history.py    history MCP tool
        inspect.py    inspect MCP tool
        status.py     status MCP tool

    oauth/
        provider.py   OAuth 2.1 authorization server
        storage.py    SQLite-backed token storage
        login.py      /login page route

doc/
    architecture.md           Two-brain model, bootstrap, synthesis design
    implementation-plan.md    Phase-by-phase build history and decisions
    entity-types.md           Entity type reference
    memory-requirements.md    Original requirements document
    tool-evaluation.md        Graphiti vs. MemoryGate evaluation

tests/
    conftest.py       Shared fixtures and --live flag
    helpers.py        Test helpers and factory functions
    test_*.py         Unit and integration tests
```

---

## Key design decisions

A few things that will confuse you if you don't know them going in:

**No OpenAI.** The provider stack is Anthropic (LLM) and Voyage AI (embeddings + reranking). OpenAI support exists in graphiti-core but is not used here and won't be added.

**All writes are async.** `remember` and `correct` enqueue tasks and return immediately. The background worker calls `graphiti.add_episode()` one at a time — Graphiti requires serial processing. Don't assume a write has landed just because the tool returned.

**Synthesis is expensive.** The synthesis agent uses Claude Opus 4.6 with adaptive thinking at high effort. A single run involves multiple tool calls, git operations, and Graphiti writes. Don't trigger it unnecessarily. The trigger threshold (`max_delta_changes`) is configurable; production defaults are conservative.

**The subject is configurable.** `subject_name` in `config/{env}.yaml` drives which Person node is the identity anchor. Vesper is the first subject, not the only possible one. All subject-specific content lives in the subject's own repo, not in this codebase.

**Protected files need manual review.** SOUL.md and IDENTITY.md are never written directly by the synthesis agent. Proposals land on the `synth/draft` branch for deliberate review. Don't merge that branch without actually reading the diff.
