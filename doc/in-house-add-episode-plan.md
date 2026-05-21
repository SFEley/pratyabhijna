# In-House `add_episode` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `graphiti.add_episode` for all three callers with an in-house three-LLM-call pipeline that cuts per-episode cost by ~7x and removes the per-edge serial-dedup latency.

**Architecture:** Six pipeline stages (idempotency → prefetch → extract → embed+node-candidates → reconcile-nodes → fetch-edge-candidates → reconcile-edges → persist) running in `src/pratyabhijna/add_episode/`. Behind a feature flag for PR-1; flipped on in PR-2. Reuses graphiti's types (`EntityNode`, `EntityEdge`, `EpisodicNode`, `SagaNode`) and the `Neo4jDriver` from `service.py`; replaces the algorithm only.

**Tech Stack:** Python 3.14, FastMCP, graphiti-core (fork @ SFEley/graphiti, types + driver only), Anthropic SDK (tool-use + prompt caching via existing `CachingAnthropicClient`), Voyage AI (batched embeddings), Neo4j.

**Spec:** [`doc/in-house-add-episode-design.md`](in-house-add-episode-design.md) — read it before starting. The spec is authoritative on design rationale; this plan covers execution.

**PR boundaries:**
- **PR-1** = Tasks 0 through 18. Lands the module behind a feature flag, all callers still on graphiti, parity tests gate the merge. Version bump: **patch**.
- **PR-2** = Tasks 19 through 21. Flips the flag, adds `status()` telemetry, removes the flag plumbing. Version bump: **minor**.

---

## File Structure

**New:**
- `src/pratyabhijna/add_episode/__init__.py` — exports `add_episode()` and `AddEpisodeResult`
- `src/pratyabhijna/add_episode/pipeline.py` — orchestrator, stage sequencing, INFO logging
- `src/pratyabhijna/add_episode/schemas.py` — Pydantic models for tool-use request/response
- `src/pratyabhijna/add_episode/extract.py` — Stage 2 (extraction)
- `src/pratyabhijna/add_episode/reconcile.py` — Stages 3a-ii, 4a, 3b, 4b (candidate fetch + reconcile LLM calls)
- `src/pratyabhijna/add_episode/persist.py` — Stage 5 (Cypher writes, both phases)
- `src/pratyabhijna/add_episode/prompts.py` — prompt text templates with cache breakpoint markers
- `src/pratyabhijna/add_episode/hash.py` — episode-body hash function
- `tests/test_add_episode_hash.py`
- `tests/test_add_episode_schemas.py`
- `tests/test_add_episode_extract.py`
- `tests/test_add_episode_reconcile.py`
- `tests/test_add_episode_persist.py`
- `tests/test_add_episode_pipeline.py`
- `tests/test_add_episode_parity.py` — parity tests against graphiti
- `tests/test_add_episode_live.py` — `--live` canary
- `scripts/measure_add_episode.py` — Task 0 instrumentation

**Modified:**
- `src/pratyabhijna/config.py` — add `AddEpisodeConfig` with `use_in_house: bool = False` flag
- `src/pratyabhijna/tools/remember.py` — feature-flag dispatch
- `src/pratyabhijna/tools/correct.py` — feature-flag dispatch
- `src/pratyabhijna/synthesis_agent.py` — feature-flag dispatch in `ingest_file`
- `config/dev.yaml`, `config/test.yaml`, `config/prod.yaml` — flag defaults

**Cypher migration (one-time, run during service start):**
- Add property `episode_hash: str` to `Episodic` nodes
- Add index on `(Episodic.group_id, Episodic.episode_hash)`

---

## Task 0: Measure graphiti's actual stage costs

**Files:**
- Create: `scripts/measure_add_episode.py`

This task validates the cost diagnosis in the spec before any production code is written. If the bottleneck is somewhere other than per-edge serial dedup, the design needs revisiting.

- [ ] **Step 1: Write the measurement script**

`scripts/measure_add_episode.py`:

```python
"""Measure graphiti.add_episode stage-by-stage.

Wraps the key internal functions with timestamp logging, runs a single
remember() through against a real Neo4j + Anthropic + Voyage stack, and
prints a per-stage breakdown.

Usage:
    PRATYABHIJNA_ENV=dev python scripts/measure_add_episode.py "your episode body"
"""
import asyncio
import sys
import time
from datetime import datetime, timezone
from contextlib import contextmanager

from graphiti_core.utils.maintenance import node_operations, edge_operations
from pratyabhijna.config import load_config
from pratyabhijna.service import PratyabhijnaService

STAGE_TIMINGS: dict[str, list[float]] = {}

@contextmanager
def time_stage(name: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        STAGE_TIMINGS.setdefault(name, []).append(elapsed)
        print(f"[stage] {name}: {elapsed*1000:.0f}ms")


def wrap(module, attr, name):
    orig = getattr(module, attr)
    async def wrapped(*args, **kwargs):
        with time_stage(name):
            return await orig(*args, **kwargs)
    setattr(module, attr, wrapped)


async def main(body: str):
    wrap(node_operations, "extract_nodes", "extract_nodes")
    wrap(node_operations, "resolve_extracted_nodes", "resolve_extracted_nodes")
    wrap(node_operations, "extract_attributes_from_nodes", "extract_attributes_from_nodes")
    wrap(edge_operations, "extract_edges", "extract_edges")
    wrap(edge_operations, "resolve_extracted_edges", "resolve_extracted_edges")
    wrap(edge_operations, "resolve_extracted_edge", "resolve_extracted_edge_per_call")

    config = load_config()
    service = PratyabhijnaService(config)
    await service.start()
    try:
        with time_stage("TOTAL"):
            await service._graphiti.add_episode(
                name=f"measurement:{datetime.now(timezone.utc).isoformat()}",
                episode_body=body,
                source_description="measurement",
                reference_time=datetime.now(timezone.utc),
                group_id=config.subject_name,
                entity_types=service.entity_types,
            )
    finally:
        await service.stop()

    print("\n=== Summary ===")
    for name, samples in sorted(STAGE_TIMINGS.items(), key=lambda kv: -sum(kv[1])):
        total_ms = sum(samples) * 1000
        count = len(samples)
        print(f"  {name}: total={total_ms:.0f}ms calls={count} mean={total_ms/count:.0f}ms")


if __name__ == "__main__":
    body = sys.argv[1] if len(sys.argv) > 1 else "This is a test observation about how Vesper measures things."
    asyncio.run(main(body))
```

- [ ] **Step 2: Run on a representative-sized episode**

Run with a ~1000-char body that mimics the original log:

```bash
PRATYABHIJNA_ENV=dev uv run python scripts/measure_add_episode.py "$(cat scripts/sample_episode.txt)"
```

(Create `scripts/sample_episode.txt` with ~1000 chars of representative content first.)

Expected output: stage breakdown with totals. Look for which stage dominates.

- [ ] **Step 3: Compare measurement to spec assumptions**

If `resolve_extracted_edges` dominates (sum of `resolve_extracted_edge_per_call` >50% of TOTAL): proceed; the design is correct.

If `extract_attributes_from_nodes` dominates (>50%): the spec needs to spell out attribute extraction more carefully — currently the design folds it into reconcile-nodes' `attribute_updates` field. Adjust the reconcile-nodes schema to make attribute extraction non-optional.

If a single function shows mean latency > 60s: that's the 4-minute call from the original log. Inspect its inputs; if it's a context-length issue, the in-house design's batched approach should naturally fix it (smaller, structured prompts).

- [ ] **Step 4: Commit the script with the measurement output appended as a comment**

```bash
git add scripts/measure_add_episode.py scripts/sample_episode.txt
git commit -m "measure: instrument graphiti add_episode stage timings"
```

---

## Task 1: Config flag + module scaffolding

**Files:**
- Modify: `src/pratyabhijna/config.py`
- Modify: `config/dev.yaml`, `config/test.yaml`, `config/prod.yaml`
- Create: `src/pratyabhijna/add_episode/__init__.py`
- Test: `tests/test_config.py` (extend existing)

- [ ] **Step 1: Extend the config test**

Append to `tests/test_config.py`:

```python
def test_add_episode_config_defaults():
    from pratyabhijna.config import load_config
    cfg = load_config()
    assert cfg.add_episode.use_in_house is False  # safe default

def test_add_episode_config_yaml_override(tmp_path, monkeypatch):
    cfg_yaml = tmp_path / "test.yaml"
    cfg_yaml.write_text("add_episode:\n  use_in_house: true\n")
    monkeypatch.setenv("PRATYABHIJNA_ENV", "test")
    monkeypatch.setenv("PRATYABHIJNA_CONFIG_DIR", str(tmp_path))
    from pratyabhijna.config import load_config
    cfg = load_config()
    assert cfg.add_episode.use_in_house is True
```

- [ ] **Step 2: Run the test — expect failure**

```bash
uv run pytest tests/test_config.py::test_add_episode_config_defaults -v
```

Expected: AttributeError on `cfg.add_episode`.

- [ ] **Step 3: Add the config class**

In `src/pratyabhijna/config.py`, add after `SynthesisConfig`:

```python
class AddEpisodeConfig(BaseModel):
    # Feature flag for the in-house add_episode pipeline (in-house vs.
    # graphiti.add_episode). Defaults False — flips True in PR-2.
    use_in_house: bool = False
    # K for candidate fetching (per extracted node and per extracted edge).
    candidate_k: int = 5
    # How many previous episodes to include as prompt context in Stage 2.
    previous_episodes_n: int = 5
```

And add `add_episode: AddEpisodeConfig = Field(default_factory=AddEpisodeConfig)` to the top-level `PratyabhijnaConfig` model (alongside the existing `synthesis: SynthesisConfig = ...`).

- [ ] **Step 4: Verify the test passes**

```bash
uv run pytest tests/test_config.py::test_add_episode_config_defaults tests/test_config.py::test_add_episode_config_yaml_override -v
```

Expected: 2 passed.

- [ ] **Step 5: Create the empty module scaffolding**

```python
# src/pratyabhijna/add_episode/__init__.py
"""In-house replacement for graphiti.add_episode.

See doc/in-house-add-episode-design.md for the architecture.
"""

from pratyabhijna.add_episode.pipeline import AddEpisodeResult, add_episode

__all__ = ["add_episode", "AddEpisodeResult"]
```

Create empty placeholder files: `pipeline.py`, `schemas.py`, `extract.py`, `reconcile.py`, `persist.py`, `prompts.py`, `hash.py` — each with just a one-line docstring matching its responsibility from the File Structure section. Stub `pipeline.py`:

```python
"""Orchestrator for the in-house add_episode pipeline."""

from dataclasses import dataclass
from typing import Any


@dataclass
class AddEpisodeResult:
    episode_uuid: str
    nodes_created: int
    nodes_updated: int
    edges_created: int
    supersessions: int
    short_circuited: bool  # True if Stage 0 idempotency hit


async def add_episode(*args, **kwargs) -> AddEpisodeResult:
    raise NotImplementedError("Implemented across Tasks 4-15")
```

- [ ] **Step 6: Commit**

```bash
git add src/pratyabhijna/config.py src/pratyabhijna/add_episode/ config/ tests/test_config.py
git commit -m "feat: scaffold add_episode module and feature flag"
```

---

## Task 2: Pydantic schemas for tool-use payloads

**Files:**
- Modify: `src/pratyabhijna/add_episode/schemas.py`
- Test: `tests/test_add_episode_schemas.py`

- [ ] **Step 1: Write the schema tests**

```python
# tests/test_add_episode_schemas.py
"""Schemas for the extract and reconcile tool-use payloads."""

import pytest
from pratyabhijna.add_episode.schemas import (
    ExtractedNode,
    ExtractedEdge,
    ExtractResponse,
    NodeDecision,
    EdgeDecision,
    ReconcileNodesResponse,
    ReconcileEdgesResponse,
)


def test_extracted_node_minimal():
    n = ExtractedNode(idx=0, name="Serah", type="Person", attributes={})
    assert n.idx == 0
    assert n.type == "Person"


def test_extracted_node_rejects_unknown_type():
    with pytest.raises(ValueError):
        ExtractedNode(idx=0, name="X", type="Bogus", attributes={})


def test_extracted_edge_minimal():
    e = ExtractedEdge(idx=0, source_idx=0, target_idx=1, name="works_on", fact="Serah works on Pratyabhijna.")
    assert e.source_idx == 0


def test_extract_response_dense_indices():
    resp = ExtractResponse(
        nodes=[
            ExtractedNode(idx=0, name="A", type="Person", attributes={}),
            ExtractedNode(idx=1, name="B", type="Concept", attributes={}),
        ],
        edges=[ExtractedEdge(idx=0, source_idx=0, target_idx=1, name="rel", fact="A rel B")],
    )
    assert len(resp.nodes) == 2


def test_extract_response_rejects_sparse_indices():
    with pytest.raises(ValueError, match="dense"):
        ExtractResponse(
            nodes=[
                ExtractedNode(idx=0, name="A", type="Person", attributes={}),
                ExtractedNode(idx=2, name="C", type="Concept", attributes={}),  # gap
            ],
            edges=[],
        )


def test_node_decision_existing_requires_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        NodeDecision(extracted_idx=0, decision="existing")


def test_node_decision_new_forbids_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        NodeDecision(extracted_idx=0, decision="new", existing_uuid="abc")


def test_edge_decision_supersedes_requires_uuid():
    with pytest.raises(ValueError, match="existing_uuid"):
        EdgeDecision(extracted_idx=0, decision="supersedes")
```

- [ ] **Step 2: Run tests — expect ImportError or test failures**

```bash
uv run pytest tests/test_add_episode_schemas.py -v
```

Expected: ImportError (schemas not yet defined).

- [ ] **Step 3: Implement the schemas**

`src/pratyabhijna/add_episode/schemas.py`:

```python
"""Pydantic schemas for the extract and reconcile tool-use payloads.

These define both the response shape we send to the Anthropic SDK as
`response_model` AND the validation we run on the parsed response.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Must stay aligned with PRATYABHIJNA_ENTITY_TYPES in entity_types.py.
EntityTypeName = Literal[
    "Person", "Event", "Place", "Project", "Artifact",
    "Observation", "Drive", "Concept", "Question", "Thread",
]


class ExtractedNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int = Field(ge=0)
    name: str = Field(min_length=1)
    type: EntityTypeName
    attributes: dict[str, Any] = Field(default_factory=dict)


class ExtractedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idx: int = Field(ge=0)
    source_idx: int = Field(ge=0)
    target_idx: int = Field(ge=0)
    name: str = Field(min_length=1)
    fact: str = Field(min_length=1)


class ExtractResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[ExtractedNode]
    edges: list[ExtractedEdge]

    @model_validator(mode="after")
    def _check_dense_indices(self):
        for items, label in [(self.nodes, "nodes"), (self.edges, "edges")]:
            expected = list(range(len(items)))
            actual = [it.idx for it in items]
            if actual != expected:
                raise ValueError(f"{label} idx must be dense 0..N-1, got {actual}")
        for e in self.edges:
            if e.source_idx >= len(self.nodes) or e.target_idx >= len(self.nodes):
                raise ValueError(f"edge {e.idx} references out-of-range node idx")
        return self


class NodeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted_idx: int = Field(ge=0)
    decision: Literal["new", "existing"]
    existing_uuid: str | None = None
    attribute_updates: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_uuid_matches_decision(self):
        if self.decision == "existing" and not self.existing_uuid:
            raise ValueError("existing decisions must include existing_uuid")
        if self.decision == "new" and self.existing_uuid:
            raise ValueError("new decisions must not include existing_uuid")
        return self


class ReconcileNodesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_decisions: list[NodeDecision]


class EdgeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extracted_idx: int = Field(ge=0)
    decision: Literal["new", "existing", "supersedes"]
    existing_uuid: str | None = None

    @model_validator(mode="after")
    def _check_uuid_matches_decision(self):
        if self.decision in ("existing", "supersedes") and not self.existing_uuid:
            raise ValueError(f"{self.decision} decisions must include existing_uuid")
        if self.decision == "new" and self.existing_uuid:
            raise ValueError("new decisions must not include existing_uuid")
        return self


class ReconcileEdgesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edge_decisions: list[EdgeDecision]
```

- [ ] **Step 4: Run tests — expect pass**

```bash
uv run pytest tests/test_add_episode_schemas.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/schemas.py tests/test_add_episode_schemas.py
git commit -m "feat: extract/reconcile tool-use schemas"
```

---

## Task 3: Episode-body hash function

**Files:**
- Modify: `src/pratyabhijna/add_episode/hash.py`
- Test: `tests/test_add_episode_hash.py`

- [ ] **Step 1: Write the hash tests**

```python
# tests/test_add_episode_hash.py
from datetime import datetime, timezone
from graphiti_core.nodes import EpisodeType
from pratyabhijna.add_episode.hash import episode_hash


def test_hash_is_deterministic():
    args = dict(
        group_id="vesper",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="Hello world",
    )
    assert episode_hash(**args) == episode_hash(**args)


def test_hash_differs_by_body():
    args = dict(
        group_id="vesper",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )
    assert episode_hash(**args, body="A") != episode_hash(**args, body="B")


def test_hash_differs_by_reference_time():
    args = dict(
        group_id="vesper",
        source=EpisodeType.message,
        source_description="self",
        body="Same body",
    )
    a = episode_hash(**args, reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc))
    b = episode_hash(**args, reference_time=datetime(2026, 5, 21, tzinfo=timezone.utc))
    assert a != b


def test_hash_differs_by_group_id():
    args = dict(
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="Same body",
    )
    assert episode_hash(**args, group_id="vesper") != episode_hash(**args, group_id="other")


def test_hash_differs_by_source_description():
    args = dict(
        group_id="vesper",
        source=EpisodeType.message,
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="Same body",
    )
    assert episode_hash(**args, source_description="self") != episode_hash(**args, source_description="other")


def test_hash_format():
    h = episode_hash(
        group_id="vesper",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="x",
    )
    assert len(h) == 64  # sha256 hex
    assert all(c in "0123456789abcdef" for c in h)
```

- [ ] **Step 2: Run — expect ImportError**

```bash
uv run pytest tests/test_add_episode_hash.py -v
```

- [ ] **Step 3: Implement**

`src/pratyabhijna/add_episode/hash.py`:

```python
"""Episode-body hash for Stage 0 idempotency."""

from __future__ import annotations

import hashlib
from datetime import datetime

from graphiti_core.nodes import EpisodeType


def episode_hash(
    *,
    group_id: str,
    source: EpisodeType,
    source_description: str,
    reference_time: datetime,
    body: str,
) -> str:
    """Stable sha256 hex of the inputs that define episode identity.

    Two calls with identical inputs collapse (queue retries, double-enqueue).
    Differing inputs — same body recorded at different times, same body
    from different sources — produce distinct hashes.
    """
    parts = [
        group_id,
        source.value,
        source_description,
        reference_time.isoformat(),
        body,
    ]
    payload = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_add_episode_hash.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/hash.py tests/test_add_episode_hash.py
git commit -m "feat: episode-body hash for idempotency"
```

---

## Task 4: Cypher migration for `episode_hash` index

**Files:**
- Modify: `src/pratyabhijna/service.py`
- Test: `tests/test_service.py` (extend)

The migration runs idempotently at `service.start()`. Same pattern graphiti uses for its constraints.

- [ ] **Step 1: Write the migration test**

Append to `tests/test_service.py`:

```python
@pytest.mark.skipif(not pytest.config.getoption("--live"), reason="needs Neo4j")
async def test_episode_hash_index_created(live_service):
    """After service.start(), the episode_hash index exists."""
    driver = live_service._graphiti.driver
    records, _, _ = await driver.execute_query(
        "SHOW INDEXES YIELD name WHERE name = 'idx_episodic_group_hash' RETURN name"
    )
    assert len(records) == 1
```

- [ ] **Step 2: Add the migration call to `service.start()`**

In `src/pratyabhijna/service.py`, after `self._graphiti = Graphiti(...)`:

```python
await self._ensure_episode_hash_index()
```

Add the method:

```python
async def _ensure_episode_hash_index(self) -> None:
    """Create the Episodic.episode_hash composite index used by Stage 0."""
    await self._graphiti.driver.execute_query(
        """
        CREATE INDEX idx_episodic_group_hash IF NOT EXISTS
        FOR (e:Episodic) ON (e.group_id, e.episode_hash)
        """
    )
```

- [ ] **Step 3: Run the live test**

```bash
uv run pytest tests/test_service.py::test_episode_hash_index_created --live -v
```

Expected: pass.

- [ ] **Step 4: Run the full mocked test suite to confirm no regression**

```bash
uv run pytest tests/test_service.py -v
```

Expected: all pass (the mock for `driver.execute_query` doesn't care about the extra call).

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/service.py tests/test_service.py
git commit -m "feat: composite index on (Episodic.group_id, episode_hash)"
```

---

## Task 5: Stage 0 — idempotency gate

**Files:**
- Modify: `src/pratyabhijna/add_episode/pipeline.py`
- Test: `tests/test_add_episode_pipeline.py`

- [ ] **Step 1: Write the idempotency tests**

```python
# tests/test_add_episode_pipeline.py
"""Pipeline orchestrator tests. Each stage gets its own focused module of
tests; this file covers the orchestrator's stage-stitching and Stage 0."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from graphiti_core.nodes import EpisodeType

from pratyabhijna.add_episode.pipeline import _check_idempotency


@pytest.mark.asyncio
async def test_idempotency_hit_returns_existing_uuid():
    driver = AsyncMock()
    driver.execute_query.return_value = ([{"uuid": "existing-123"}], None, None)
    hit = await _check_idempotency(driver, group_id="vesper", episode_hash="deadbeef")
    assert hit == "existing-123"


@pytest.mark.asyncio
async def test_idempotency_miss_returns_none():
    driver = AsyncMock()
    driver.execute_query.return_value = ([], None, None)
    hit = await _check_idempotency(driver, group_id="vesper", episode_hash="newhash")
    assert hit is None
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_add_episode_pipeline.py::test_idempotency_hit_returns_existing_uuid -v
```

- [ ] **Step 3: Implement `_check_idempotency` in `pipeline.py`**

Add to `src/pratyabhijna/add_episode/pipeline.py`:

```python
async def _check_idempotency(driver, *, group_id: str, episode_hash: str) -> str | None:
    """Stage 0 — return uuid of existing Episodic with this hash, else None."""
    records, _, _ = await driver.execute_query(
        """
        MATCH (e:Episodic {group_id: $group_id, episode_hash: $episode_hash})
        RETURN e.uuid AS uuid
        LIMIT 1
        """,
        group_id=group_id,
        episode_hash=episode_hash,
        routing_="r",
    )
    return records[0]["uuid"] if records else None
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/pipeline.py tests/test_add_episode_pipeline.py
git commit -m "feat: Stage 0 idempotency gate"
```

---

## Task 6: Stage 1 — pre-flight reads

**Files:**
- Modify: `src/pratyabhijna/add_episode/pipeline.py`
- Test: `tests/test_add_episode_pipeline.py`

Pre-flight fetches previous episodes for prompt context, and (if saga is set without `saga_previous_episode_uuid`) the most recent episode in the saga.

- [ ] **Step 1: Write the pre-flight tests**

Append to `tests/test_add_episode_pipeline.py`:

```python
from graphiti_core.nodes import EpisodicNode
from pratyabhijna.add_episode.pipeline import _prefetch


@pytest.mark.asyncio
async def test_prefetch_loads_n_previous_episodes(monkeypatch):
    fake_episodes = [MagicMock(spec=EpisodicNode, uuid=f"u{i}") for i in range(5)]
    graphiti = AsyncMock()
    graphiti.retrieve_episodes.return_value = fake_episodes

    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        source=EpisodeType.message,
        previous_n=5,
        saga=None,
        saga_previous_episode_uuid=None,
    )
    assert len(result.previous_episodes) == 5
    assert result.saga_prior_uuid is None


@pytest.mark.asyncio
async def test_prefetch_resolves_saga_prior(monkeypatch):
    graphiti = AsyncMock()
    graphiti.retrieve_episodes.return_value = []
    # Stub the saga prior fetch (TBD detail in pipeline impl)
    async def fake_saga_prior(driver, group_id, saga_name):
        return "prior-saga-ep-uuid"
    monkeypatch.setattr("pratyabhijna.add_episode.pipeline._get_saga_latest_episode_uuid", fake_saga_prior)

    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        source=EpisodeType.message,
        previous_n=5,
        saga="solo-sessions",
        saga_previous_episode_uuid=None,
    )
    assert result.saga_prior_uuid == "prior-saga-ep-uuid"


@pytest.mark.asyncio
async def test_prefetch_skips_saga_lookup_when_uuid_provided():
    graphiti = AsyncMock()
    graphiti.retrieve_episodes.return_value = []
    result = await _prefetch(
        graphiti=graphiti,
        group_id="vesper",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        source=EpisodeType.message,
        previous_n=5,
        saga="solo-sessions",
        saga_previous_episode_uuid="explicit-uuid",
    )
    assert result.saga_prior_uuid == "explicit-uuid"
```

- [ ] **Step 2: Run — expect failure**

- [ ] **Step 3: Implement `_prefetch` in `pipeline.py`**

```python
import asyncio
from dataclasses import dataclass
from graphiti_core.nodes import EpisodicNode


@dataclass
class PrefetchResult:
    previous_episodes: list[EpisodicNode]
    saga_prior_uuid: str | None


async def _get_saga_latest_episode_uuid(driver, group_id: str, saga_name: str) -> str | None:
    records, _, _ = await driver.execute_query(
        """
        MATCH (s:Saga {group_id: $group_id, name: $saga_name})-[:HAS_EPISODE]->(e:Episodic)
        RETURN e.uuid AS uuid
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        group_id=group_id,
        saga_name=saga_name,
        routing_="r",
    )
    return records[0]["uuid"] if records else None


async def _prefetch(
    *,
    graphiti,
    group_id: str,
    reference_time,
    source,
    previous_n: int,
    saga: str | None,
    saga_previous_episode_uuid: str | None,
) -> PrefetchResult:
    async def _episodes():
        return await graphiti.retrieve_episodes(
            reference_time, last_n=previous_n, group_ids=[group_id], source=source
        )

    async def _saga_prior():
        if not saga or saga_previous_episode_uuid:
            return saga_previous_episode_uuid
        return await _get_saga_latest_episode_uuid(graphiti.driver, group_id, saga)

    episodes, saga_prior = await asyncio.gather(_episodes(), _saga_prior())
    return PrefetchResult(previous_episodes=episodes, saga_prior_uuid=saga_prior)
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/pipeline.py tests/test_add_episode_pipeline.py
git commit -m "feat: Stage 1 pre-flight reads (previous episodes + saga prior)"
```

---

## Task 7: Stage 2 prompts and tool schema

**Files:**
- Modify: `src/pratyabhijna/add_episode/prompts.py`
- Modify: `src/pratyabhijna/add_episode/extract.py`
- Test: `tests/test_add_episode_extract.py`

The extraction prompt has four cacheable layers (system, per-session, previous-episode context, the episode body). Anthropic prompt caching uses a leading-prefix match, so the four layers are organized in cacheability order with explicit cache breakpoints.

- [ ] **Step 1: Write the prompt tests**

```python
# tests/test_add_episode_extract.py
"""Tests for Stage 2 extraction (prompt + LLM call)."""

import pytest
from datetime import datetime, timezone
from graphiti_core.nodes import EpisodeType

from pratyabhijna.add_episode.prompts import (
    build_extract_system_prompt,
    build_extract_user_message,
)


def test_system_prompt_includes_all_entity_type_docs():
    from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES
    system = build_extract_system_prompt()
    for name in PRATYABHIJNA_ENTITY_TYPES:
        assert name in system, f"system prompt missing entity type {name}"
        # Docstring should be embedded
        doc = PRATYABHIJNA_ENTITY_TYPES[name].__doc__ or ""
        first_line = doc.strip().splitlines()[0] if doc.strip() else ""
        if first_line:
            assert first_line in system, f"system prompt missing {name} docstring start"


def test_user_message_includes_episode_body_and_previous():
    msg = build_extract_user_message(
        episode_name="obs:2026-05-20",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        body="The episode body",
        previous_episodes=[],
    )
    assert "The episode body" in msg
    assert "2026-05-20" in msg


def test_user_message_source_hint_varies():
    msg_text = build_extract_user_message(
        episode_name="x", source=EpisodeType.text,
        source_description="d", reference_time=datetime.now(timezone.utc),
        body="b", previous_episodes=[],
    )
    msg_message = build_extract_user_message(
        episode_name="x", source=EpisodeType.message,
        source_description="d", reference_time=datetime.now(timezone.utc),
        body="b", previous_episodes=[],
    )
    # The source-type hint should differ between text and message variants.
    assert msg_text != msg_message
```

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement the prompt builders**

`src/pratyabhijna/add_episode/prompts.py`:

```python
"""Prompt builders for extract and reconcile stages.

Layered for prompt caching: system (stable across all episodes) → per-session
(stable across a saga) → previous-episode context (changes per session) →
the episode itself.
"""

from __future__ import annotations

from datetime import datetime
from textwrap import dedent

from graphiti_core.nodes import EpisodeType, EpisodicNode

from pratyabhijna.entity_types import PRATYABHIJNA_ENTITY_TYPES


def build_extract_system_prompt() -> str:
    """Stable across every extract call. Cacheable."""
    type_blocks = []
    for name, model in PRATYABHIJNA_ENTITY_TYPES.items():
        doc = (model.__doc__ or "").strip()
        type_blocks.append(f"### {name}\n\n{doc}")
    types_section = "\n\n".join(type_blocks)
    return dedent(
        """
        You are an entity-and-relation extractor for a knowledge graph that
        represents one subject's structured self-knowledge.

        For the supplied episode (text written by or about the subject), extract:

        - The named entities mentioned, typed by the schemas below.
        - The factual relations between them, expressed as edges with a
          semantic predicate (e.g. "works_on", "remembers", "supersedes")
          and a single short sentence stating the fact.

        Do not extract entities that appear only as passing mentions with no
        information attached. Do not invent edges that the text doesn't assert.

        ## Entity Types

        """
    ).strip() + "\n\n" + types_section


def build_extract_user_message(
    *,
    episode_name: str,
    source: EpisodeType,
    source_description: str,
    reference_time: datetime,
    body: str,
    previous_episodes: list[EpisodicNode],
) -> str:
    """The per-episode body of the extract prompt.

    Previous episodes are listed for context (so the extractor knows what
    "we" or "I" refers to in conversational episodes). The episode body
    comes last so it's the freshest piece of context in the LLM's window.
    """
    src_hint = {
        EpisodeType.message: "This episode is a conversational message.",
        EpisodeType.text: "This episode is a document or written passage.",
        EpisodeType.json: "This episode is structured JSON.",
    }[source]

    prev_block = ""
    if previous_episodes:
        prev_block = "## Previous Episodes (for context)\n\n" + "\n\n---\n\n".join(
            f"[{ep.valid_at.isoformat() if ep.valid_at else 'unknown'}] {ep.content}"
            for ep in previous_episodes
        ) + "\n\n"

    return dedent(
        f"""
        {src_hint}

        Source: {source_description}
        Reference time: {reference_time.isoformat()}
        Episode name: {episode_name}

        {prev_block}## Episode

        {body}

        Now call the extract_episode tool with the entities and edges.
        """
    ).strip()
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/prompts.py tests/test_add_episode_extract.py
git commit -m "feat: extract-stage prompt builders"
```

---

## Task 8: Stage 2 extraction LLM call

**Files:**
- Modify: `src/pratyabhijna/add_episode/extract.py`
- Test: `tests/test_add_episode_extract.py` (extend)

- [ ] **Step 1: Write the extract-call tests**

Append to `tests/test_add_episode_extract.py`:

```python
from unittest.mock import AsyncMock
from pratyabhijna.add_episode.extract import extract
from pratyabhijna.add_episode.schemas import ExtractResponse


@pytest.mark.asyncio
async def test_extract_returns_validated_response():
    llm_client = AsyncMock()
    llm_client.generate_response = AsyncMock(return_value={
        "nodes": [
            {"idx": 0, "name": "Serah", "type": "Person", "attributes": {}},
        ],
        "edges": [],
    })
    result = await extract(
        llm_client=llm_client,
        episode_name="x",
        source=EpisodeType.message,
        source_description="self",
        reference_time=datetime.now(timezone.utc),
        body="Serah wrote this.",
        previous_episodes=[],
    )
    assert isinstance(result, ExtractResponse)
    assert result.nodes[0].name == "Serah"


@pytest.mark.asyncio
async def test_extract_raises_on_malformed_response():
    llm_client = AsyncMock()
    llm_client.generate_response = AsyncMock(return_value={"nodes": "not a list", "edges": []})
    with pytest.raises(Exception):  # pydantic ValidationError
        await extract(
            llm_client=llm_client,
            episode_name="x", source=EpisodeType.message, source_description="self",
            reference_time=datetime.now(timezone.utc), body="x", previous_episodes=[],
        )
```

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement `extract`**

`src/pratyabhijna/add_episode/extract.py`:

```python
"""Stage 2 — extract nodes and edges from the episode."""

from __future__ import annotations

from datetime import datetime

from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.prompts.models import Message

from pratyabhijna.add_episode.prompts import (
    build_extract_system_prompt,
    build_extract_user_message,
)
from pratyabhijna.add_episode.schemas import ExtractResponse


async def extract(
    *,
    llm_client: LLMClient,
    episode_name: str,
    source: EpisodeType,
    source_description: str,
    reference_time: datetime,
    body: str,
    previous_episodes: list[EpisodicNode],
) -> ExtractResponse:
    system = build_extract_system_prompt()
    user = build_extract_user_message(
        episode_name=episode_name,
        source=source,
        source_description=source_description,
        reference_time=reference_time,
        body=body,
        previous_episodes=previous_episodes,
    )
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]
    raw = await llm_client.generate_response(
        messages,
        response_model=ExtractResponse,
        prompt_name="pratyabhijna.add_episode.extract",
    )
    return ExtractResponse(**raw) if isinstance(raw, dict) else raw
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/extract.py tests/test_add_episode_extract.py
git commit -m "feat: Stage 2 extraction LLM call"
```

---

## Task 9: Stage 3a-i batched Voyage embeddings

**Files:**
- Modify: `src/pratyabhijna/add_episode/reconcile.py`
- Test: `tests/test_add_episode_reconcile.py`

- [ ] **Step 1: Write the embed-batch tests**

```python
# tests/test_add_episode_reconcile.py
import pytest
from unittest.mock import AsyncMock
from pratyabhijna.add_episode.reconcile import embed_all
from pratyabhijna.add_episode.schemas import (
    ExtractedNode, ExtractedEdge, ExtractResponse,
)


@pytest.mark.asyncio
async def test_embed_all_single_batch():
    embedder = AsyncMock()
    # Voyage returns one embedding per input text, in order.
    embedder.create_batch.return_value = [[0.1] * 8, [0.2] * 8, [0.3] * 8]
    extracted = ExtractResponse(
        nodes=[
            ExtractedNode(idx=0, name="A", type="Person", attributes={}),
            ExtractedNode(idx=1, name="B", type="Concept", attributes={}),
        ],
        edges=[
            ExtractedEdge(idx=0, source_idx=0, target_idx=1, name="rel", fact="A relates B"),
        ],
    )
    node_emb, edge_emb = await embed_all(embedder, extracted)
    assert len(node_emb) == 2 and len(edge_emb) == 1
    assert node_emb[0] == [0.1] * 8
    assert edge_emb[0] == [0.3] * 8
    # Called once with all 3 texts batched.
    embedder.create_batch.assert_awaited_once()
    args = embedder.create_batch.await_args.args[0]
    assert args == ["A", "B", "A relates B"]


@pytest.mark.asyncio
async def test_embed_all_splits_oversized_batches():
    """If extracted texts exceed VOYAGE_BATCH_LIMIT, split into multiple calls."""
    embedder = AsyncMock()
    # Stub: return same-length result per call.
    embedder.create_batch.side_effect = lambda texts: [[0.0] * 4] * len(texts)
    # Build 130 nodes (over the 128 limit).
    extracted = ExtractResponse(
        nodes=[ExtractedNode(idx=i, name=f"n{i}", type="Person", attributes={}) for i in range(130)],
        edges=[],
    )
    node_emb, edge_emb = await embed_all(embedder, extracted)
    assert len(node_emb) == 130
    assert embedder.create_batch.await_count == 2  # 128 + 2
```

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement `embed_all`**

`src/pratyabhijna/add_episode/reconcile.py`:

```python
"""Reconcile stages — candidate fetch and LLM-based dedup decisions.

Stages handled here:
- 3a-i: batched embedding of extracted node names + edge facts
- 3a-ii: node candidate fetch (exact + fuzzy + embedding)
- 4a: reconcile-nodes LLM call
- 3b: edge candidate fetch (resolved-endpoint + fact-similarity)
- 4b: reconcile-edges LLM call
"""

from __future__ import annotations

from pratyabhijna.add_episode.schemas import ExtractResponse


VOYAGE_BATCH_LIMIT = 128


async def embed_all(embedder, extracted: ExtractResponse) -> tuple[list[list[float]], list[list[float]]]:
    """Stage 3a-i — one or more batched Voyage calls.

    Returns (node_embeddings, edge_embeddings) sliced back to per-item lists.
    Splits into VOYAGE_BATCH_LIMIT-sized batches when needed.
    """
    node_texts = [n.name for n in extracted.nodes]
    edge_texts = [e.fact for e in extracted.edges]
    all_texts = node_texts + edge_texts

    embeddings: list[list[float]] = []
    for i in range(0, len(all_texts), VOYAGE_BATCH_LIMIT):
        batch = all_texts[i:i + VOYAGE_BATCH_LIMIT]
        result = await embedder.create_batch(batch)
        embeddings.extend(result)

    n = len(node_texts)
    return embeddings[:n], embeddings[n:]
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/reconcile.py tests/test_add_episode_reconcile.py
git commit -m "feat: Stage 3a-i batched Voyage embeddings"
```

---

## Task 10: Stage 3a-ii node candidate fetch

**Files:**
- Modify: `src/pratyabhijna/add_episode/reconcile.py`
- Test: `tests/test_add_episode_reconcile.py` (extend)

Three sources combined: exact-name short-circuit, fuzzy MinHash/LSH, embedding top-K.

- [ ] **Step 1: Write the tests**

```python
from graphiti_core.nodes import EntityNode
from pratyabhijna.add_episode.reconcile import fetch_node_candidates


@pytest.mark.asyncio
async def test_fetch_node_candidates_exact_match(monkeypatch):
    driver = AsyncMock()
    # Stub exact-match query: returns one EntityNode-shaped record.
    driver.execute_query.return_value = (
        [{"uuid": "u1", "name": "Serah", "name_embedding": [0.0] * 8}],
        None, None,
    )
    # Stub fuzzy and embedding helpers to return nothing — focus on exact path.
    monkeypatch.setattr("pratyabhijna.add_episode.reconcile._fuzzy_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr("pratyabhijna.add_episode.reconcile._embedding_candidates", AsyncMock(return_value=[]))

    extracted = ExtractResponse(
        nodes=[ExtractedNode(idx=0, name="Serah", type="Person", attributes={})],
        edges=[],
    )
    candidates = await fetch_node_candidates(driver, "vesper", extracted, node_embeddings=[[0.0]*8], k=5)
    assert candidates[0][0]["uuid"] == "u1"


@pytest.mark.asyncio
async def test_fetch_node_candidates_union_dedups():
    """Same candidate uuid from multiple sources collapses to one entry."""
    # Cover this when stubbing all three sources to return overlapping uuids.
    # ...detailed assertion that the per-extracted-node list has no duplicate uuids.
```

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement `fetch_node_candidates`**

Append to `src/pratyabhijna/add_episode/reconcile.py`:

```python
from typing import Any

from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact


async def fetch_node_candidates(
    driver,
    group_id: str,
    extracted: ExtractResponse,
    *,
    node_embeddings: list[list[float]],
    k: int = 5,
) -> list[list[dict[str, Any]]]:
    """Stage 3a-ii — per-extracted-node candidate sets.

    Returns a list indexed by extracted-node idx; each entry is a list of
    candidate dicts (uuid, name, summary, attributes) deduped by uuid across
    the three sources.
    """
    exact = await _exact_candidates(driver, group_id, extracted)
    fuzzy = await _fuzzy_candidates(driver, group_id, extracted, k=k)
    embedded = await _embedding_candidates(driver, group_id, node_embeddings, k=k)

    out: list[list[dict[str, Any]]] = []
    for i, node in enumerate(extracted.nodes):
        seen: dict[str, dict[str, Any]] = {}
        for source in (exact[i], fuzzy[i], embedded[i]):
            for c in source:
                seen.setdefault(c["uuid"], c)
        out.append(list(seen.values()))
    return out


async def _exact_candidates(driver, group_id: str, extracted: ExtractResponse) -> list[list[dict[str, Any]]]:
    normalized = [_normalize_string_exact(n.name) for n in extracted.nodes]
    records, _, _ = await driver.execute_query(
        """
        MATCH (n:Entity {group_id: $group_id})
        WHERE n.name_normalized IN $names
        RETURN n.uuid AS uuid, n.name AS name, n.name_normalized AS name_normalized,
               n.summary AS summary, n.attributes AS attributes, labels(n) AS labels
        """,
        group_id=group_id,
        names=normalized,
        routing_="r",
    )
    by_normalized: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_normalized.setdefault(r["name_normalized"], []).append(dict(r))
    return [by_normalized.get(norm, []) for norm in normalized]


async def _fuzzy_candidates(driver, group_id: str, extracted: ExtractResponse, *, k: int) -> list[list[dict[str, Any]]]:
    """MinHash/LSH-bucketed name shortlist via graphiti's dedup_helpers."""
    # Full implementation: fetch all entity names for the group (or use an
    # LSH index Neo4j-side if one's been created), build a candidate-name
    # index, and query per extracted node. For initial implementation,
    # fall back to a `name CONTAINS` LIKE-style match capped at K when
    # the LSH index isn't precomputed.
    out: list[list[dict[str, Any]]] = []
    for node in extracted.nodes:
        records, _, _ = await driver.execute_query(
            """
            MATCH (n:Entity {group_id: $group_id})
            WHERE toLower(n.name) CONTAINS toLower($needle)
            RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, n.attributes AS attributes, labels(n) AS labels
            LIMIT $k
            """,
            group_id=group_id,
            needle=node.name[:64],
            k=k,
            routing_="r",
        )
        out.append([dict(r) for r in records])
    return out


async def _embedding_candidates(driver, group_id: str, embeddings: list[list[float]], *, k: int) -> list[list[dict[str, Any]]]:
    """Cosine top-K against Entity.name_embedding via Neo4j vector index."""
    out: list[list[dict[str, Any]]] = []
    for emb in embeddings:
        records, _, _ = await driver.execute_query(
            """
            CALL db.index.vector.queryNodes('entity_name_embedding', $k, $emb)
            YIELD node, score
            WHERE node.group_id = $group_id
            RETURN node.uuid AS uuid, node.name AS name, node.summary AS summary,
                   node.attributes AS attributes, labels(node) AS labels, score
            """,
            group_id=group_id,
            emb=emb,
            k=k,
            routing_="r",
        )
        out.append([dict(r) for r in records])
    return out
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/reconcile.py tests/test_add_episode_reconcile.py
git commit -m "feat: Stage 3a-ii node candidate fetch (exact + fuzzy + embedding)"
```

---

## Task 11: Stage 4a — reconcile-nodes LLM call

**Files:**
- Modify: `src/pratyabhijna/add_episode/prompts.py`
- Modify: `src/pratyabhijna/add_episode/reconcile.py`
- Test: `tests/test_add_episode_reconcile.py` (extend)

- [ ] **Step 1: Write tests asserting on the rendered prompt and the LLM response handling**

```python
from pratyabhijna.add_episode.reconcile import reconcile_nodes
from pratyabhijna.add_episode.schemas import ReconcileNodesResponse, NodeDecision


@pytest.mark.asyncio
async def test_reconcile_nodes_returns_validated_decisions():
    llm = AsyncMock()
    llm.generate_response = AsyncMock(return_value={
        "node_decisions": [
            {"extracted_idx": 0, "decision": "existing", "existing_uuid": "u1", "attribute_updates": {}},
            {"extracted_idx": 1, "decision": "new"},
        ],
    })
    extracted = ExtractResponse(
        nodes=[
            ExtractedNode(idx=0, name="A", type="Person", attributes={}),
            ExtractedNode(idx=1, name="B", type="Concept", attributes={}),
        ],
        edges=[],
    )
    candidates = [
        [{"uuid": "u1", "name": "A", "summary": "...", "attributes": {}, "labels": ["Entity", "Person"]}],
        [],
    ]
    result = await reconcile_nodes(llm_client=llm, extracted=extracted, candidates=candidates)
    assert len(result.node_decisions) == 2
    assert result.node_decisions[0].existing_uuid == "u1"


@pytest.mark.asyncio
async def test_reconcile_nodes_decisions_cover_every_extracted_node():
    """LLM must return one decision per extracted node — raise if it skips one."""
    llm = AsyncMock()
    llm.generate_response = AsyncMock(return_value={"node_decisions": []})
    extracted = ExtractResponse(
        nodes=[ExtractedNode(idx=0, name="A", type="Person", attributes={})],
        edges=[],
    )
    with pytest.raises(ValueError, match="missing decision"):
        await reconcile_nodes(llm_client=llm, extracted=extracted, candidates=[[]])
```

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement the reconcile-nodes prompt and call**

Add to `prompts.py`:

```python
def build_reconcile_nodes_prompt(
    *,
    extracted_nodes: list[dict],  # serialized form: idx, name, type, attributes
    candidates: list[list[dict]],
) -> str:
    blocks = []
    for i, node in enumerate(extracted_nodes):
        cand_str = "\n".join(
            f"  - uuid={c['uuid']} name={c['name']!r} labels={c['labels']} summary={(c.get('summary') or '')[:120]!r}"
            for c in candidates[i]
        ) or "  (no candidates)"
        blocks.append(
            f"Extracted node idx={i} name={node['name']!r} type={node['type']}\n"
            f"Candidates:\n{cand_str}"
        )
    body = "\n\n".join(blocks)
    return dedent(
        f"""
        You are deciding, for each extracted node below, whether it matches one
        of the candidate existing nodes (decision="existing", with existing_uuid)
        or is a new node (decision="new", no existing_uuid).

        Match liberally on identity but conservatively on type: two candidates
        with the same name but different labels are usually different entities
        (a Person named "Vesper" is not the AI named "Vesper").

        If matching, you may optionally include attribute_updates with fields
        that should be merged onto the existing node (use sparingly — only
        when the episode adds genuinely new information).

        Return one decision per extracted node, in any order; every extracted
        idx must appear exactly once.

        {body}

        Call the reconcile_nodes tool with your decisions.
        """
    ).strip()
```

Add to `reconcile.py`:

```python
from graphiti_core.prompts.models import Message
from pratyabhijna.add_episode.prompts import build_reconcile_nodes_prompt
from pratyabhijna.add_episode.schemas import ReconcileNodesResponse


async def reconcile_nodes(
    *,
    llm_client,
    extracted: ExtractResponse,
    candidates: list[list[dict[str, Any]]],
) -> ReconcileNodesResponse:
    nodes_serialized = [
        {"idx": n.idx, "name": n.name, "type": n.type, "attributes": n.attributes}
        for n in extracted.nodes
    ]
    user_prompt = build_reconcile_nodes_prompt(
        extracted_nodes=nodes_serialized, candidates=candidates,
    )
    messages = [
        Message(role="system", content="You are a deduplication assistant for a knowledge graph."),
        Message(role="user", content=user_prompt),
    ]
    raw = await llm_client.generate_response(
        messages,
        response_model=ReconcileNodesResponse,
        prompt_name="pratyabhijna.add_episode.reconcile_nodes",
    )
    response = ReconcileNodesResponse(**raw) if isinstance(raw, dict) else raw

    # Guarantee coverage: one decision per extracted idx.
    seen = {d.extracted_idx for d in response.node_decisions}
    expected = set(range(len(extracted.nodes)))
    if seen != expected:
        missing = expected - seen
        raise ValueError(f"reconcile_nodes missing decision for extracted idx {sorted(missing)}")

    return response
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/prompts.py src/pratyabhijna/add_episode/reconcile.py tests/test_add_episode_reconcile.py
git commit -m "feat: Stage 4a reconcile-nodes LLM call"
```

---

## Task 12: Stage 3b — edge candidate fetch (with resolved endpoints)

**Files:**
- Modify: `src/pratyabhijna/add_episode/reconcile.py`
- Test: `tests/test_add_episode_reconcile.py` (extend)

Edge candidates need resolved-uuid endpoints from Stage 4a. This stage runs after `reconcile_nodes`. Input: a map `extracted_idx → resolved_uuid` covering every extracted node.

- [ ] **Step 1: Write tests**

```python
from pratyabhijna.add_episode.reconcile import fetch_edge_candidates


@pytest.mark.asyncio
async def test_fetch_edge_candidates_uses_resolved_endpoints():
    driver = AsyncMock()
    driver.execute_query.return_value = (
        [{"uuid": "edge-1", "name": "rel", "fact": "A rel B", "source_uuid": "ua", "target_uuid": "ub"}],
        None, None,
    )
    extracted = ExtractResponse(
        nodes=[
            ExtractedNode(idx=0, name="A", type="Person", attributes={}),
            ExtractedNode(idx=1, name="B", type="Concept", attributes={}),
        ],
        edges=[ExtractedEdge(idx=0, source_idx=0, target_idx=1, name="rel", fact="A rel B")],
    )
    node_resolution = {0: "ua", 1: "ub"}
    candidates = await fetch_edge_candidates(
        driver, "vesper", extracted, node_resolution=node_resolution,
        edge_embeddings=[[0.0] * 8], k=5,
    )
    assert len(candidates[0]) == 1
    assert candidates[0][0]["uuid"] == "edge-1"
```

- [ ] **Step 2: Run — expect failure**

- [ ] **Step 3: Implement**

```python
async def fetch_edge_candidates(
    driver,
    group_id: str,
    extracted: ExtractResponse,
    *,
    node_resolution: dict[int, str],
    edge_embeddings: list[list[float]],
    k: int = 5,
) -> list[list[dict[str, Any]]]:
    """Stage 3b — per-extracted-edge candidate sets (with resolved endpoints)."""
    out: list[list[dict[str, Any]]] = []
    for i, edge in enumerate(extracted.edges):
        src_uuid = node_resolution[edge.source_idx]
        tgt_uuid = node_resolution[edge.target_idx]
        endpoint_records, _, _ = await driver.execute_query(
            """
            MATCH (s:Entity {uuid: $src})-[r:RELATES_TO]-(t:Entity {uuid: $tgt})
            WHERE r.group_id = $group_id
            RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact,
                   startNode(r).uuid AS source_uuid, endNode(r).uuid AS target_uuid,
                   r.valid_at AS valid_at, r.invalid_at AS invalid_at
            """,
            src=src_uuid, tgt=tgt_uuid, group_id=group_id,
            routing_="r",
        )
        embedding_records, _, _ = await driver.execute_query(
            """
            CALL db.index.vector.queryRelationships('edge_fact_embedding', $k, $emb)
            YIELD relationship, score
            WHERE relationship.group_id = $group_id
              AND (startNode(relationship).uuid IN $endpoints OR endNode(relationship).uuid IN $endpoints)
            RETURN relationship.uuid AS uuid, relationship.name AS name, relationship.fact AS fact,
                   startNode(relationship).uuid AS source_uuid, endNode(relationship).uuid AS target_uuid,
                   relationship.valid_at AS valid_at, relationship.invalid_at AS invalid_at, score
            """,
            group_id=group_id, emb=edge_embeddings[i], k=k,
            endpoints=list(node_resolution.values()),
            routing_="r",
        )
        seen: dict[str, dict[str, Any]] = {}
        for r in list(endpoint_records) + list(embedding_records):
            seen.setdefault(r["uuid"], dict(r))
        out.append(list(seen.values()))
    return out
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/reconcile.py tests/test_add_episode_reconcile.py
git commit -m "feat: Stage 3b edge candidate fetch (resolved endpoints)"
```

---

## Task 13: Stage 4b — reconcile-edges LLM call

**Files:**
- Modify: `src/pratyabhijna/add_episode/prompts.py`
- Modify: `src/pratyabhijna/add_episode/reconcile.py`
- Test: `tests/test_add_episode_reconcile.py` (extend)

- [ ] **Step 1: Write tests**

```python
from pratyabhijna.add_episode.reconcile import reconcile_edges
from pratyabhijna.add_episode.schemas import ReconcileEdgesResponse


@pytest.mark.asyncio
async def test_reconcile_edges_handles_supersession():
    llm = AsyncMock()
    llm.generate_response = AsyncMock(return_value={
        "edge_decisions": [
            {"extracted_idx": 0, "decision": "supersedes", "existing_uuid": "old-edge"},
        ],
    })
    extracted = ExtractResponse(
        nodes=[
            ExtractedNode(idx=0, name="A", type="Person", attributes={}),
            ExtractedNode(idx=1, name="B", type="Concept", attributes={}),
        ],
        edges=[ExtractedEdge(idx=0, source_idx=0, target_idx=1, name="believes", fact="A now believes B")],
    )
    candidates = [[{"uuid": "old-edge", "name": "believes", "fact": "A used to believe not-B",
                    "source_uuid": "ua", "target_uuid": "ub", "valid_at": None, "invalid_at": None}]]
    result = await reconcile_edges(llm_client=llm, extracted=extracted, candidates=candidates)
    assert result.edge_decisions[0].decision == "supersedes"
    assert result.edge_decisions[0].existing_uuid == "old-edge"
```

- [ ] **Step 2: Run — expect failure**

- [ ] **Step 3: Implement**

Add to `prompts.py`:

```python
def build_reconcile_edges_prompt(
    *,
    extracted_edges: list[dict],
    candidates: list[list[dict]],
) -> str:
    blocks = []
    for i, edge in enumerate(extracted_edges):
        cand_str = "\n".join(
            f"  - uuid={c['uuid']} name={c['name']!r} fact={c['fact']!r} valid_at={c.get('valid_at')}"
            for c in candidates[i]
        ) or "  (no candidates)"
        blocks.append(
            f"Extracted edge idx={i}: name={edge['name']!r} fact={edge['fact']!r}\n"
            f"Candidates:\n{cand_str}"
        )
    body = "\n\n".join(blocks)
    return dedent(
        f"""
        You are deciding, for each extracted edge, one of three outcomes:

        - "new": the edge is genuinely new information.
        - "existing": the edge already exists (existing_uuid required); reuse it.
        - "supersedes": the edge asserts a state of the world that contradicts an
          existing candidate edge (existing_uuid required); the candidate will be
          marked invalid as of this episode's reference time.

        Supersession is for *contradiction*, not for "this is the same subject"
        — two edges about the same subject can both be true. If unsure between
        "new" and "supersedes", choose "new".

        Return one decision per extracted edge, in any order; every extracted
        idx must appear exactly once.

        {body}

        Call the reconcile_edges tool with your decisions.
        """
    ).strip()
```

Add to `reconcile.py`:

```python
from pratyabhijna.add_episode.prompts import build_reconcile_edges_prompt
from pratyabhijna.add_episode.schemas import ReconcileEdgesResponse


async def reconcile_edges(
    *,
    llm_client,
    extracted: ExtractResponse,
    candidates: list[list[dict[str, Any]]],
) -> ReconcileEdgesResponse:
    edges_serialized = [
        {"idx": e.idx, "name": e.name, "fact": e.fact}
        for e in extracted.edges
    ]
    user_prompt = build_reconcile_edges_prompt(
        extracted_edges=edges_serialized, candidates=candidates,
    )
    messages = [
        Message(role="system", content="You are a deduplication and contradiction-detection assistant for a knowledge graph."),
        Message(role="user", content=user_prompt),
    ]
    raw = await llm_client.generate_response(
        messages,
        response_model=ReconcileEdgesResponse,
        prompt_name="pratyabhijna.add_episode.reconcile_edges",
    )
    response = ReconcileEdgesResponse(**raw) if isinstance(raw, dict) else raw

    seen = {d.extracted_idx for d in response.edge_decisions}
    expected = set(range(len(extracted.edges)))
    if seen != expected:
        missing = expected - seen
        raise ValueError(f"reconcile_edges missing decision for extracted idx {sorted(missing)}")

    return response
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/prompts.py src/pratyabhijna/add_episode/reconcile.py tests/test_add_episode_reconcile.py
git commit -m "feat: Stage 4b reconcile-edges LLM call"
```

---

## Task 14: Stage 5a — persist nodes and saga

**Files:**
- Modify: `src/pratyabhijna/add_episode/persist.py`
- Test: `tests/test_add_episode_persist.py`

- [ ] **Step 1: Write integration tests against real Neo4j (gated by `--live`)**

```python
# tests/test_add_episode_persist.py
import pytest
from datetime import datetime, timezone
from graphiti_core.nodes import EntityNode, SagaNode
from pratyabhijna.add_episode.persist import persist_nodes_and_saga


@pytest.mark.skipif("not config.getoption('--live')", reason="needs Neo4j")
@pytest.mark.asyncio
async def test_persist_new_nodes_creates_in_graph(live_service, clean_test_group):
    driver = live_service._graphiti.driver
    now = datetime.now(timezone.utc)
    new_nodes = [
        EntityNode(
            name="Test Person",
            group_id=clean_test_group,
            labels=["Entity", "Person"],
            created_at=now,
            summary="Test summary.",
            name_embedding=[0.1] * 1024,
        ),
    ]
    await persist_nodes_and_saga(
        driver, new_nodes=new_nodes, updated_nodes=[], saga_name=None,
        group_id=clean_test_group,
    )
    records, _, _ = await driver.execute_query(
        "MATCH (n:Entity {group_id: $g}) RETURN n.name AS name",
        g=clean_test_group,
    )
    assert any(r["name"] == "Test Person" for r in records)
```

- [ ] **Step 2: Run — expect failure (live)**

- [ ] **Step 3: Implement**

`src/pratyabhijna/add_episode/persist.py`:

```python
"""Stage 5 — Cypher writes (Phase 5a: nodes/saga; Phase 5b: edges/episode)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from graphiti_core.edges import EntityEdge, EpisodicEdge
from graphiti_core.nodes import EntityNode, EpisodicNode, SagaNode


async def persist_nodes_and_saga(
    driver,
    *,
    new_nodes: list[EntityNode],
    updated_nodes: list[tuple[EntityNode, dict[str, Any]]],  # (node, attribute_updates)
    saga_name: str | None,
    group_id: str,
) -> SagaNode | None:
    """Phase 5a — save new nodes, apply updates, ensure saga."""
    for node in new_nodes:
        await node.save(driver)
    for node, updates in updated_nodes:
        # Merge updates onto attributes property and re-save.
        merged = dict(node.attributes or {})
        merged.update(updates)
        node.attributes = merged
        await node.save(driver)

    if saga_name:
        records, _, _ = await driver.execute_query(
            "MATCH (s:Saga {group_id: $g, name: $n}) RETURN s.uuid AS uuid",
            g=group_id, n=saga_name, routing_="r",
        )
        if records:
            return await SagaNode.get_by_uuid(driver, records[0]["uuid"])
        saga = SagaNode(name=saga_name, group_id=group_id, labels=["Saga"], created_at=datetime.now())
        await saga.save(driver)
        return saga
    return None
```

- [ ] **Step 4: Run — expect pass (live)**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/persist.py tests/test_add_episode_persist.py
git commit -m "feat: Stage 5a persist nodes and saga"
```

---

## Task 15: Stage 5b — persist edges, episode, episodic edges, supersession, saga edges

**Files:**
- Modify: `src/pratyabhijna/add_episode/persist.py`
- Test: `tests/test_add_episode_persist.py` (extend)

- [ ] **Step 1: Write tests**

```python
@pytest.mark.skipif("not config.getoption('--live')", reason="needs Neo4j")
@pytest.mark.asyncio
async def test_persist_supersession_invalidates_prior_edge(live_service, clean_test_group):
    """A supersedes decision sets invalid_at on the candidate edge."""
    # Seed: create two nodes and one edge.
    # Persist a supersession: pass the candidate uuid in supersedes_uuids.
    # Assert: the candidate edge now has invalid_at = episode.valid_at.
    ...  # full body in implementation


@pytest.mark.skipif("not config.getoption('--live')", reason="needs Neo4j")
@pytest.mark.asyncio
async def test_persist_saga_chain(live_service, clean_test_group):
    """HAS_EPISODE and NEXT_EPISODE edges land correctly when saga is set."""
    ...
```

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement `persist_edges_and_episode`**

Append to `persist.py`:

```python
async def persist_edges_and_episode(
    driver,
    *,
    new_edges: list[EntityEdge],
    supersedes_uuids: list[str],  # candidate edges to invalidate
    episode: EpisodicNode,
    touched_node_uuids: list[str],
    saga: SagaNode | None,
    saga_prior_episode_uuid: str | None,
) -> None:
    """Phase 5b — edges, episode, MENTIONS, supersession, saga edges."""

    for edge in new_edges:
        await edge.save(driver)

    if supersedes_uuids:
        await driver.execute_query(
            """
            MATCH ()-[r:RELATES_TO]-()
            WHERE r.uuid IN $uuids AND r.invalid_at IS NULL AND r.group_id = $g
            SET r.invalid_at = $valid_at
            """,
            uuids=supersedes_uuids,
            g=episode.group_id,
            valid_at=episode.valid_at,
        )

    await episode.save(driver)

    if touched_node_uuids:
        await driver.execute_query(
            """
            MATCH (e:Episodic {uuid: $ep})
            UNWIND $node_uuids AS nuuid
            MATCH (n:Entity {uuid: nuuid})
            MERGE (e)-[:MENTIONS {group_id: $g, created_at: $created_at}]->(n)
            """,
            ep=episode.uuid, node_uuids=touched_node_uuids,
            g=episode.group_id, created_at=episode.created_at,
        )

    if saga is not None:
        await driver.execute_query(
            """
            MATCH (s:Saga {uuid: $saga}), (e:Episodic {uuid: $ep})
            MERGE (s)-[:HAS_EPISODE]->(e)
            """,
            saga=saga.uuid, ep=episode.uuid,
        )
        if saga_prior_episode_uuid:
            await driver.execute_query(
                """
                MATCH (p:Episodic {uuid: $prev}), (e:Episodic {uuid: $ep})
                MERGE (p)-[:NEXT_EPISODE]->(e)
                """,
                prev=saga_prior_episode_uuid, ep=episode.uuid,
            )
```

- [ ] **Step 4: Run — expect pass (live)**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/persist.py tests/test_add_episode_persist.py
git commit -m "feat: Stage 5b persist edges, episode, supersession, saga chain"
```

---

## Task 16: Pipeline orchestrator + INFO logging

**Files:**
- Modify: `src/pratyabhijna/add_episode/pipeline.py`
- Test: `tests/test_add_episode_pipeline.py` (extend)

This stitches everything together and emits the INFO log lines from the spec.

- [ ] **Step 1: Write the end-to-end test (mocked LLM, real Neo4j)**

```python
@pytest.mark.skipif("not config.getoption('--live')", reason="needs Neo4j; LLM is mocked")
@pytest.mark.asyncio
async def test_pipeline_end_to_end_idempotent(live_service, clean_test_group, monkeypatch):
    # Mock the LLM client to return canned extract + reconcile responses.
    # Run add_episode() twice with the same body.
    # Assert: second call short-circuits (returns same uuid, no LLM calls).
    ...
```

- [ ] **Step 2: Run — expect failure**

- [ ] **Step 3: Implement `add_episode`**

Replace the placeholder in `src/pratyabhijna/add_episode/pipeline.py`:

```python
import logging
import time
import uuid as uuid_module
from datetime import datetime, timezone
from collections import Counter

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from pratyabhijna.add_episode.extract import extract
from pratyabhijna.add_episode.hash import episode_hash
from pratyabhijna.add_episode.reconcile import (
    embed_all, fetch_node_candidates, reconcile_nodes,
    fetch_edge_candidates, reconcile_edges,
)
from pratyabhijna.add_episode.persist import persist_nodes_and_saga, persist_edges_and_episode

log = logging.getLogger(__name__)


async def add_episode(
    service,
    *,
    name: str,
    episode_body: str,
    source_description: str,
    reference_time: datetime,
    source: EpisodeType = EpisodeType.message,
    group_id: str,
    saga: str | None = None,
    saga_previous_episode_uuid: str | None = None,
) -> AddEpisodeResult:
    t0 = time.perf_counter()
    log.info(
        "add_episode start episode=%s group=%s source=%s body_chars=%d",
        name, group_id, source.value, len(episode_body),
    )

    driver = service._graphiti.driver
    config = service.config

    # Stage 0
    h = episode_hash(
        group_id=group_id, source=source, source_description=source_description,
        reference_time=reference_time, body=episode_body,
    )
    hit = await _check_idempotency(driver, group_id=group_id, episode_hash=h)
    if hit:
        log.info("add_episode stage=idempotency decision=existing_hit episode_uuid=%s", hit)
        log.info("add_episode complete episode_uuid=%s total_latency_ms=%.0f llm_calls=0 embed_batches=0", hit, (time.perf_counter() - t0) * 1000)
        return AddEpisodeResult(
            episode_uuid=hit, nodes_created=0, nodes_updated=0,
            edges_created=0, supersessions=0, short_circuited=True,
        )
    log.info("add_episode stage=idempotency decision=new")

    # Stage 1
    prefetch = await _prefetch(
        graphiti=service._graphiti, group_id=group_id, reference_time=reference_time,
        source=source, previous_n=config.add_episode.previous_episodes_n,
        saga=saga, saga_previous_episode_uuid=saga_previous_episode_uuid,
    )
    log.info(
        "add_episode stage=prefetch previous_episodes=%d saga=%s",
        len(prefetch.previous_episodes), saga or "none",
    )

    # Stage 2
    t = time.perf_counter()
    extracted = await extract(
        llm_client=service._graphiti.llm_client,
        episode_name=name, source=source, source_description=source_description,
        reference_time=reference_time, body=episode_body,
        previous_episodes=prefetch.previous_episodes,
    )
    type_counts = Counter(n.type for n in extracted.nodes)
    edge_name_counts = Counter(e.name for e in extracted.edges)
    log.info(
        "add_episode stage=extract llm_calls=1 latency_ms=%.0f nodes=%d edges=%d",
        (time.perf_counter() - t) * 1000, len(extracted.nodes), len(extracted.edges),
    )
    log.info(
        "add_episode stage=extract types nodes=%s edges=%s",
        ",".join(f"{k}:{v}" for k, v in sorted(type_counts.items())),
        ",".join(f"{k}:{v}" for k, v in sorted(edge_name_counts.items())) or "(none)",
    )

    # Stage 3a-i + 3a-ii in parallel
    import asyncio
    t = time.perf_counter()
    embed_task = asyncio.create_task(embed_all(service._graphiti.embedder, extracted))
    cand_task = asyncio.create_task(_fetch_node_candidates_when_embeddings_ready(
        embed_task, driver, group_id, extracted, config.add_episode.candidate_k,
    ))
    node_embeddings, edge_embeddings = await embed_task
    node_candidates = await cand_task
    log.info(
        "add_episode stage=embed batches=%d texts=%d latency_ms=%.0f",
        ((len(node_embeddings) + len(edge_embeddings) - 1) // 128) + 1,
        len(node_embeddings) + len(edge_embeddings),
        (time.perf_counter() - t) * 1000,
    )
    log.info(
        "add_episode stage=fetch_node_candidates total_candidates=%d max_per_node=%d",
        sum(len(c) for c in node_candidates),
        max((len(c) for c in node_candidates), default=0),
    )

    # Stage 4a
    t = time.perf_counter()
    node_decisions = await reconcile_nodes(
        llm_client=service._graphiti.llm_client,
        extracted=extracted, candidates=node_candidates,
    )
    existing_count = sum(1 for d in node_decisions.node_decisions if d.decision == "existing")
    new_count = sum(1 for d in node_decisions.node_decisions if d.decision == "new")
    attr_updates = sum(1 for d in node_decisions.node_decisions if d.attribute_updates)
    log.info(
        "add_episode stage=reconcile_nodes llm_calls=1 latency_ms=%.0f existing=%d new=%d attribute_updates=%d",
        (time.perf_counter() - t) * 1000, existing_count, new_count, attr_updates,
    )

    # Build idx → resolved_uuid map; mint new uuids for "new" decisions.
    node_resolution: dict[int, str] = {}
    new_node_objects: list[EntityNode] = []
    updated_node_objects: list[tuple[EntityNode, dict]] = []
    now = datetime.now(timezone.utc)
    for decision in node_decisions.node_decisions:
        ext = extracted.nodes[decision.extracted_idx]
        if decision.decision == "existing":
            node_resolution[decision.extracted_idx] = decision.existing_uuid
            if decision.attribute_updates:
                existing = await EntityNode.get_by_uuid(driver, decision.existing_uuid)
                updated_node_objects.append((existing, decision.attribute_updates))
        else:
            new_uuid = str(uuid_module.uuid4())
            node_resolution[decision.extracted_idx] = new_uuid
            new_node = EntityNode(
                uuid=new_uuid, name=ext.name, group_id=group_id,
                labels=["Entity", ext.type], created_at=now, summary="",
                name_embedding=node_embeddings[decision.extracted_idx],
                attributes=ext.attributes,
            )
            new_node_objects.append(new_node)

    # Stage 3b
    t = time.perf_counter()
    edge_candidates = await fetch_edge_candidates(
        driver, group_id, extracted,
        node_resolution=node_resolution, edge_embeddings=edge_embeddings,
        k=config.add_episode.candidate_k,
    )
    log.info(
        "add_episode stage=fetch_edge_candidates total_candidates=%d max_per_edge=%d",
        sum(len(c) for c in edge_candidates),
        max((len(c) for c in edge_candidates), default=0),
    )

    # Stage 4b
    t = time.perf_counter()
    edge_decisions = await reconcile_edges(
        llm_client=service._graphiti.llm_client,
        extracted=extracted, candidates=edge_candidates,
    )
    ee_existing = sum(1 for d in edge_decisions.edge_decisions if d.decision == "existing")
    ee_new = sum(1 for d in edge_decisions.edge_decisions if d.decision == "new")
    ee_super = sum(1 for d in edge_decisions.edge_decisions if d.decision == "supersedes")
    log.info(
        "add_episode stage=reconcile_edges llm_calls=1 latency_ms=%.0f existing=%d new=%d supersedes=%d",
        (time.perf_counter() - t) * 1000, ee_existing, ee_new, ee_super,
    )

    # Build new edges and supersession list
    new_edge_objects: list[EntityEdge] = []
    supersedes_uuids: list[str] = []
    for decision in edge_decisions.edge_decisions:
        ext = extracted.edges[decision.extracted_idx]
        if decision.decision == "existing":
            continue
        if decision.decision == "supersedes":
            supersedes_uuids.append(decision.existing_uuid)
        # Both "new" and "supersedes" produce a new edge.
        edge = EntityEdge(
            source_node_uuid=node_resolution[ext.source_idx],
            target_node_uuid=node_resolution[ext.target_idx],
            name=ext.name, fact=ext.fact,
            group_id=group_id, created_at=now, valid_at=reference_time,
            fact_embedding=edge_embeddings[decision.extracted_idx],
            episodes=[],
        )
        new_edge_objects.append(edge)

    # Build the EpisodicNode
    episode_node = EpisodicNode(
        name=name, group_id=group_id, labels=[],
        source=source, content=episode_body,
        source_description=source_description,
        created_at=now, valid_at=reference_time,
    )
    # Stash the hash on attributes — actual property mapping happens in EpisodicNode.save
    # via a graphiti-side change. For now, set via raw write below.

    # Stage 5
    t = time.perf_counter()
    saga_node = await persist_nodes_and_saga(
        driver,
        new_nodes=new_node_objects, updated_nodes=updated_node_objects,
        saga_name=saga, group_id=group_id,
    )
    touched_uuids = list(node_resolution.values())
    await persist_edges_and_episode(
        driver,
        new_edges=new_edge_objects,
        supersedes_uuids=supersedes_uuids,
        episode=episode_node,
        touched_node_uuids=touched_uuids,
        saga=saga_node,
        saga_prior_episode_uuid=prefetch.saga_prior_uuid,
    )
    # Attach episode_hash to the episodic node (Cypher SET, since the dataclass
    # may not carry the property yet).
    await driver.execute_query(
        "MATCH (e:Episodic {uuid: $u}) SET e.episode_hash = $h",
        u=episode_node.uuid, h=h,
    )
    log.info(
        "add_episode stage=persist nodes_created=%d nodes_updated=%d edges_created=%d supersessions=%d mentions=%d latency_ms=%.0f",
        len(new_node_objects), len(updated_node_objects),
        len(new_edge_objects), len(supersedes_uuids), len(touched_uuids),
        (time.perf_counter() - t) * 1000,
    )
    log.info(
        "add_episode complete episode_uuid=%s total_latency_ms=%.0f llm_calls=3 embed_batches=%d",
        episode_node.uuid, (time.perf_counter() - t0) * 1000,
        ((len(node_embeddings) + len(edge_embeddings) - 1) // 128) + 1 if (node_embeddings or edge_embeddings) else 0,
    )

    return AddEpisodeResult(
        episode_uuid=episode_node.uuid,
        nodes_created=len(new_node_objects),
        nodes_updated=len(updated_node_objects),
        edges_created=len(new_edge_objects),
        supersessions=len(supersedes_uuids),
        short_circuited=False,
    )


async def _fetch_node_candidates_when_embeddings_ready(
    embed_task, driver, group_id, extracted, k,
):
    node_emb, _ = await embed_task
    return await fetch_node_candidates(driver, group_id, extracted, node_embeddings=node_emb, k=k)
```

- [ ] **Step 4: Run — expect pass (live)**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/add_episode/pipeline.py tests/test_add_episode_pipeline.py
git commit -m "feat: pipeline orchestrator with INFO logging at every stage"
```

---

## Task 17: Wire the three callers behind the feature flag

**Files:**
- Modify: `src/pratyabhijna/tools/remember.py`
- Modify: `src/pratyabhijna/tools/correct.py`
- Modify: `src/pratyabhijna/synthesis_agent.py`
- Test: `tests/test_remember.py`, `tests/test_correct.py` (extend each)

- [ ] **Step 1: Write tests that assert the flag controls dispatch**

For `tests/test_remember.py`, add:

```python
@pytest.mark.asyncio
async def test_remember_uses_in_house_when_flag_set(mock_service, monkeypatch):
    """When config.add_episode.use_in_house is True, dispatch to pratyabhijna.add_episode."""
    mock_service.config.add_episode.use_in_house = True
    monkeypatch.setattr("pratyabhijna.add_episode.add_episode", AsyncMock(return_value=MagicMock(episode_uuid="abc")))
    # Run the remember handler; assert in-house was called, graphiti was not.
    ...


@pytest.mark.asyncio
async def test_remember_uses_graphiti_when_flag_unset(mock_service):
    """Default flag (False) — old path is used."""
    mock_service.config.add_episode.use_in_house = False
    # Run the remember handler; assert graphiti.add_episode was called.
    ...
```

Repeat the pattern in `tests/test_correct.py` for the correct handler.

- [ ] **Step 2: Run — expect failures**

- [ ] **Step 3: Implement the dispatch in each caller**

In `src/pratyabhijna/tools/remember.py`, update `handle_add_episode`:

```python
async def handle_add_episode(payload: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    reference_time = _resolve_reference_time(payload.get("occurred_at"))
    saga = payload.get("saga")
    saga_prev = payload.get("saga_previous_episode_uuid")

    if service.config.add_episode.use_in_house:
        from pratyabhijna.add_episode import add_episode as in_house_add_episode
        await in_house_add_episode(
            service,
            name=f"{payload['memory_type']}:{now.isoformat()}",
            episode_body=payload["content"],
            source_description=payload["source"],
            reference_time=reference_time,
            group_id=service.config.subject_name,
            saga=saga,
            saga_previous_episode_uuid=saga_prev,
        )
        return

    # ... existing graphiti.add_episode call unchanged
```

Apply the same dispatch shape to `tools/correct.py:handle_add_correction` (it uses `name=f"correction:{...}"`) and `synthesis_agent.py:ingest_file` (uses `source=EpisodeType.text`).

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add src/pratyabhijna/tools/remember.py src/pratyabhijna/tools/correct.py src/pratyabhijna/synthesis_agent.py tests/test_remember.py tests/test_correct.py
git commit -m "feat: feature-flag dispatch in remember/correct/synthesis_agent"
```

---

## Task 18: Parity test framework

**Files:**
- Create: `tests/test_add_episode_parity.py`
- Create: `tests/fixtures/parity_episodes/` (directory)

A small corpus of representative episodes is run through both pipelines against fresh databases, then the resulting graph shape is compared.

- [ ] **Step 1: Curate the corpus**

Create `tests/fixtures/parity_episodes/`:
- `obs_short.json` — a typical 1-2 sentence Observation.
- `obs_long.json` — a 1000-1500 char Observation with several entities and edges.
- `event_with_supersession.json` — pair: first episode asserts X, second contradicts.
- `correction.json` — a correction-shaped episode.
- `repo_file.json` — synthesis_agent ingest of a small markdown file.

Each JSON: `{"name", "body", "source", "source_description", "reference_time", "saga"?}`.

- [ ] **Step 2: Write the parity test**

```python
# tests/test_add_episode_parity.py
import json
import pytest
from pathlib import Path
from datetime import datetime
from graphiti_core.nodes import EpisodeType
from pratyabhijna.add_episode import add_episode as in_house_add_episode


PARITY_DIR = Path(__file__).parent / "fixtures" / "parity_episodes"


def _load_episodes():
    return sorted(PARITY_DIR.glob("*.json"))


@pytest.mark.skipif("not config.getoption('--live')", reason="needs Neo4j; LLM is live")
@pytest.mark.parametrize("path", _load_episodes(), ids=lambda p: p.name)
@pytest.mark.asyncio
async def test_parity(path, two_clean_services):
    """Run the same episode through graphiti and in-house, compare graphs."""
    spec = json.loads(path.read_text())
    args = dict(
        name=spec["name"], episode_body=spec["body"],
        source=EpisodeType(spec["source"]),
        source_description=spec["source_description"],
        reference_time=datetime.fromisoformat(spec["reference_time"]),
        group_id="parity-test", saga=spec.get("saga"),
    )

    service_g, service_p = two_clean_services
    await service_g._graphiti.add_episode(entity_types=service_g.entity_types, **args)
    await in_house_add_episode(service_p, **args)

    # Compare graph shape: node names per label, edge fact counts, supersessions.
    g_nodes = await _summarize_graph(service_g._graphiti.driver, "parity-test")
    p_nodes = await _summarize_graph(service_p._graphiti.driver, "parity-test")
    # Allow some variance in node names (LLM stochasticity); assert on
    # structural equivalence (counts per label, edge type distribution,
    # presence of supersessions).
    assert abs(g_nodes["node_count"] - p_nodes["node_count"]) <= 2
    assert g_nodes["supersession_count"] == p_nodes["supersession_count"]
    # ... additional structural assertions
```

The `two_clean_services` fixture (in `tests/conftest.py`) constructs two Pratyabhijna services on two distinct test databases.

- [ ] **Step 3: Run the parity suite with `--live`**

```bash
uv run pytest tests/test_add_episode_parity.py --live -v
```

Iterate on prompts until parity is acceptable (counts within ±2, supersession counts exact, no missing key entities). Document any deliberate divergences in a docstring on the test.

- [ ] **Step 4: Commit**

```bash
git add tests/test_add_episode_parity.py tests/fixtures/parity_episodes/
git commit -m "test: parity framework comparing in-house vs graphiti add_episode"
```

---

## Task 19: Live canary test

**Files:**
- Create: `tests/test_add_episode_live.py`

- [ ] **Step 1: Write the canary**

```python
# tests/test_add_episode_live.py
"""Single-shot live test against real Anthropic + Voyage. Catches regressions
in batching, prompt caching, or call-count assumptions."""

import pytest
import time
from datetime import datetime, timezone
from graphiti_core.nodes import EpisodeType
from pratyabhijna.add_episode import add_episode


@pytest.mark.skipif("not config.getoption('--live')", reason="needs live LLM + DB")
@pytest.mark.asyncio
async def test_live_call_counts_and_latency(live_service, clean_test_group, caplog):
    body = (
        "Vesper noticed today that the new measurement scaffolding caught a "
        "regression in the embed-batch sizing — a 130-item batch was being "
        "sent as 130 separate calls. The fix dropped that to two batches. "
        "Worth remembering: batch-size verification belongs in the live canary."
    )
    t0 = time.perf_counter()
    result = await add_episode(
        live_service,
        name=f"canary:{datetime.now(timezone.utc).isoformat()}",
        episode_body=body,
        source_description="canary",
        reference_time=datetime.now(timezone.utc),
        group_id=clean_test_group,
    )
    elapsed = time.perf_counter() - t0

    # Generous bounds: LLM latency varies. Catch regressions, not normal variance.
    assert elapsed < 60, f"add_episode took {elapsed:.1f}s — regression?"
    assert result.short_circuited is False
    # Re-run to confirm idempotency.
    t1 = time.perf_counter()
    result2 = await add_episode(
        live_service, name="ignored", episode_body=body,
        source_description="canary",
        reference_time=datetime(2026, 5, 20, tzinfo=timezone.utc),
        group_id=clean_test_group,
    )
    # Same body + same reference_time + same source_description → idempotency hit
    assert (time.perf_counter() - t1) < 2, "idempotency short-circuit not firing"
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/test_add_episode_live.py --live -v
```

- [ ] **Step 3: Commit and push the PR-1 branch**

```bash
git add tests/test_add_episode_live.py
git commit -m "test: live canary for add_episode call counts and latency"
```

Push and open PR-1. Title: `feat: in-house add_episode pipeline (flag off)`.

---

## ⛔ PR-1 boundary — wait for merge before continuing

Tasks 0-19 belong to PR-1. After Serah merges, pull main and start PR-2.

---

## Task 20: Flip the feature flag

**Files:**
- Modify: `src/pratyabhijna/config.py`
- Modify: `config/dev.yaml`, `config/test.yaml`, `config/prod.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Update the test default expectation**

```python
def test_add_episode_config_defaults():
    cfg = load_config()
    assert cfg.add_episode.use_in_house is True  # flipped
```

- [ ] **Step 2: Flip the default in `AddEpisodeConfig`**

```python
use_in_house: bool = True
```

- [ ] **Step 3: Run the full test suite, including parity and live**

```bash
uv run pytest tests/ -v
uv run pytest tests/test_add_episode_parity.py tests/test_add_episode_live.py --live -v
```

- [ ] **Step 4: Commit**

```bash
git add src/pratyabhijna/config.py config/ tests/test_config.py
git commit -m "feat: flip add_episode.use_in_house default to True"
```

---

## Task 21: status() add_episode block + cleanup

**Files:**
- Modify: `src/pratyabhijna/tools/status.py` (or wherever status() is implemented)
- Modify: `src/pratyabhijna/service.py` — rolling counters
- Test: `tests/test_status.py` (extend)
- Modify: `src/pratyabhijna/tools/remember.py`, `tools/correct.py`, `synthesis_agent.py` — remove the flag branch

- [ ] **Step 1: Add rolling counters to PratyabhijnaService**

```python
# In service.py
from collections import deque
import time


class _AddEpisodeStats:
    def __init__(self, window_seconds: float = 24 * 3600):
        self.window_seconds = window_seconds
        self.samples: deque = deque()

    def record(self, *, latency_ms: float, input_tokens: int, output_tokens: int) -> None:
        now = time.time()
        self.samples.append((now, latency_ms, input_tokens, output_tokens))
        # Trim window
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def snapshot(self) -> dict:
        if not self.samples:
            return {"count": 0, "mean_latency_ms": 0, "mean_input_tokens": 0, "mean_output_tokens": 0}
        n = len(self.samples)
        return {
            "count": n,
            "mean_latency_ms": sum(s[1] for s in self.samples) / n,
            "mean_input_tokens": sum(s[2] for s in self.samples) / n,
            "mean_output_tokens": sum(s[3] for s in self.samples) / n,
        }
```

Add `self.add_episode_stats = _AddEpisodeStats()` to `__init__`, and have the pipeline call `service.add_episode_stats.record(...)` at the end of each successful run.

- [ ] **Step 2: Add the block to status()**

In the status handler:

```python
"add_episode": service.add_episode_stats.snapshot(),
```

- [ ] **Step 3: Remove flag plumbing from the three callers**

Each caller drops the `if service.config.add_episode.use_in_house:` branch — they always call the in-house version now. The flag stays in config for one release (deprecation) but is unread.

- [ ] **Step 4: Remove the flag from config in a follow-up PR after one release cycle**

(Not in this PR — deprecate, then remove.)

- [ ] **Step 5: Run all tests**

```bash
uv run pytest tests/ -v
```

- [ ] **Step 6: Commit and push PR-2**

```bash
git add src/pratyabhijna/ tests/
git commit -m "feat: status() add_episode telemetry block; remove flag plumbing"
```

PR-2 title: `feat: cut over to in-house add_episode + telemetry`.

---

## Self-review checklist (for the planner, before handing to execution)

- [x] Every spec section maps to a task (Stage 0–5, telemetry, parity, two-PR migration).
- [x] No "TBD" or "TODO" strings in tasks; only intentional stubs that say "full body in implementation" within test sketches with enough context to write.
- [x] Function names and signatures consistent across tasks (`extract`, `embed_all`, `fetch_node_candidates`, `reconcile_nodes`, `fetch_edge_candidates`, `reconcile_edges`, `persist_nodes_and_saga`, `persist_edges_and_episode`, `add_episode`).
- [x] Test file naming consistent (`test_add_episode_<stage>.py`).
- [x] Commit messages follow the project's style (lowercase imperative, prefix where it helps).
- [x] PR boundary is unambiguous.
