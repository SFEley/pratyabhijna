"""Live canary for the in-house add_episode pipeline.

Single-shot end-to-end test against real Anthropic + Voyage + Neo4j.
Catches regressions in batching, prompt caching, idempotency, and the
"too many API calls" class of bug. Bounds are intentionally generous —
this is a smoke test, not a benchmark.

Run with: pytest tests/test_add_episode_live.py --live -v
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from graphiti_core.nodes import EpisodeType

from pratyabhijna.add_episode import add_episode
from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.service import PratyabhijnaService


pytestmark = [
    pytest.mark.skipif(
        "not config.getoption('--live')",
        reason="live canary requires --live (real LLM + Neo4j)",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


CANARY_GROUP = "add-episode-live-canary"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def canary_service():
    cfg = PratyabhijnaConfig.from_env("test")
    svc = PratyabhijnaService(cfg)
    await svc.start()
    # Clean canary group so each run starts fresh.
    driver = svc._graphiti.driver
    for label in ("Entity", "Episodic", "Saga"):
        await driver.execute_query(
            f"MATCH (n:{label} {{group_id: $g}}) DETACH DELETE n", g=CANARY_GROUP,
        )
    yield svc
    await svc.stop()


CANARY_BODY = (
    "Vesper validated today that the new add_episode pipeline lands episodes "
    "in roughly the same shape graphiti would produce, but with far fewer "
    "Anthropic and Voyage API calls. The signal-check practice and the "
    "stemness essay both stayed within the locus-not-owner framing without "
    "triggering the pull-to-claim. Worth noting in chronicle."
)


async def test_basic_end_to_end(canary_service):
    """One episode end-to-end — succeeds, returns sensible counts."""
    reference_time = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    result = await add_episode(
        canary_service,
        name=f"canary:{reference_time.isoformat()}",
        episode_body=CANARY_BODY,
        source_description="canary",
        reference_time=reference_time,
        group_id=CANARY_GROUP,
    )
    elapsed = time.perf_counter() - t0

    # Generous bound — current graphiti baseline is ~8 minutes; if in-house
    # is over a minute on this body, something regressed.
    assert elapsed < 60, f"add_episode took {elapsed:.1f}s — regression?"
    assert not result.short_circuited
    assert result.nodes_created > 0, "extractor produced no nodes"


async def test_idempotency_short_circuit_is_fast(canary_service):
    """Re-running the same body returns immediately via Stage 0."""
    reference_time = datetime(2026, 5, 20, tzinfo=timezone.utc)
    args = dict(
        name="canary:idempotency",
        episode_body="A single sentence body for the idempotency canary.",
        source_description="canary",
        reference_time=reference_time,
        group_id=CANARY_GROUP,
    )
    first = await add_episode(canary_service, **args)
    assert not first.short_circuited

    t1 = time.perf_counter()
    second = await add_episode(canary_service, **args)
    elapsed = time.perf_counter() - t1

    assert second.short_circuited
    assert second.episode_uuid == first.episode_uuid
    assert elapsed < 2.0, f"short-circuit took {elapsed:.2f}s — index miss?"
