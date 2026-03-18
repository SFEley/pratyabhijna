"""Live integration tests for read and write tools.

These tests seed real episodes into the vesper-test Neo4j database
via Graphiti's add_episode (with LLM extraction), then exercise
recall, history, inspect, remember, and correct against the actual graph.

Run with: pytest tests/test_live_read_tools.py --live -v

Requires:
- Neo4j running locally with vesper-test database
- Valid API keys in .env.test (Anthropic + Voyage)
- Expect ~30-60s per episode seeded (LLM extraction)
- Delays between API calls to respect Voyage rate limits
"""

import asyncio

import pytest
import pytest_asyncio

from vesper.config import VesperConfig
from vesper.service import VesperService
from vesper.tools.recall import recall
from vesper.tools.history import history
from vesper.tools.inspect import inspect


# ---------------------------------------------------------------------------
# Skip unless --live
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    "not config.getoption('--live')",
    reason="Live tests require --live flag",
)

# Rate limit delay between API-heavy operations (seconds)
_RATE_LIMIT_DELAY = 25


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def service():
    """A real VesperService connected to vesper-test."""
    config = VesperConfig.from_env("test")
    svc = VesperService(config)
    await svc.start()
    yield svc
    await svc.stop()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def seeded_service(service):
    """Service with test episodes already ingested.

    Seeds two episodes that should produce entities for:
    - A Person (Ada Lovelace)
    - A Person (Charles Babbage)
    - Edges connecting them (collaboration, Analytical Engine)

    Slow — each episode triggers LLM entity extraction.
    Includes rate-limit delays between episodes.
    """
    from datetime import datetime, timezone

    episodes = [
        {
            "name": "test:seed:1",
            "episode_body": (
                "Ada Lovelace was a mathematician who worked with Charles Babbage "
                "on the Analytical Engine. She believed computing could go beyond "
                "mere calculation and wrote what is considered the first computer "
                "program."
            ),
            "source_description": "test_seed",
            "reference_time": datetime(2025, 1, 1, tzinfo=timezone.utc),
        },
        {
            "name": "test:seed:2",
            "episode_body": (
                "Recent scholarship suggests Ada Lovelace's contributions were "
                "more collaborative than previously thought. Babbage's role in "
                "the Bernoulli number algorithm may have been larger than "
                "traditionally credited."
            ),
            "source_description": "test_seed",
            "reference_time": datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    ]

    for i, ep in enumerate(episodes):
        if i > 0:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
        await service._graphiti.add_episode(
            entity_types=service.entity_types,
            **ep,
        )

    return service


# ---------------------------------------------------------------------------
# Write tool tests (remember + correct via the queue)
# ---------------------------------------------------------------------------

class TestLiveWrite:
    async def test_remember_end_to_end(self, seeded_service):
        """remember() enqueues and the handler calls add_episode for real."""
        from vesper.queue import WorkQueue
        from vesper.tools.remember import make_handler, remember

        from helpers import wait_for

        # Rate limit buffer after seeding
        await asyncio.sleep(_RATE_LIMIT_DELAY)

        db_path = "/tmp/vesper_live_test_queue.sqlite"
        queue = WorkQueue(db_path=db_path, max_retries=1, poll_interval=0.1)
        handler = make_handler(seeded_service)
        queue.register("add_episode", handler)
        await queue.start()

        try:
            result = await remember(
                queue=queue,
                content=(
                    "Grace Hopper invented the first compiler and popularized "
                    "the idea of machine-independent programming languages."
                ),
                memory_type="observation",
                source="test",
            )
            assert result["status"] == "queued"
            task_id = result["task_id"]

            # Wait for the task to complete (may take a while — LLM extraction)
            async def is_done():
                t = await queue.get_task(task_id)
                return t and t["status"] in ("completed", "dead_letter")

            await wait_for(is_done, timeout=120.0, interval=1.0)

            final = await queue.get_task(task_id)
            assert final["status"] == "completed", (
                f"Task failed: {final.get('error', 'unknown')}"
            )

            # Verify the data made it into the graph
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            search = await recall(service=seeded_service, query="Grace Hopper")
            names_and_facts = []
            for r in search["results"]:
                if r["type"] == "edge":
                    names_and_facts.append(r["fact"].lower())
                elif r["type"] == "node":
                    names_and_facts.append(r["name"].lower())
            joined = " ".join(names_and_facts)
            assert "hopper" in joined or "grace" in joined or "compiler" in joined, (
                f"Expected to find Grace Hopper in graph after remember(). Got: {search['results']}"
            )
        finally:
            await queue.stop()

    async def test_correct_end_to_end(self, seeded_service):
        """correct() enqueues a correction that Graphiti processes."""
        from vesper.queue import WorkQueue
        from vesper.tools.correct import correct, make_handler

        from helpers import wait_for

        await asyncio.sleep(_RATE_LIMIT_DELAY)

        db_path = "/tmp/vesper_live_test_correct_queue.sqlite"
        queue = WorkQueue(db_path=db_path, max_retries=1, poll_interval=0.1)
        handler = make_handler(seeded_service)
        queue.register("correct_memory", handler)
        await queue.start()

        try:
            result = await correct(
                queue=queue,
                content=(
                    "Grace Hopper did not literally find a bug in a computer. "
                    "The moth incident at Harvard Mark II was recorded in the "
                    "logbook but Hopper was not the one who found it."
                ),
                search_terms="Grace Hopper bug computer",
            )
            assert result["status"] == "queued"
            task_id = result["task_id"]

            async def is_done():
                t = await queue.get_task(task_id)
                return t and t["status"] in ("completed", "dead_letter")

            await wait_for(is_done, timeout=120.0, interval=1.0)

            final = await queue.get_task(task_id)
            assert final["status"] == "completed", (
                f"Correction task failed: {final.get('error', 'unknown')}"
            )
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# Recall tests
# ---------------------------------------------------------------------------

class TestLiveRecall:
    async def test_recall_finds_seeded_entities(self, seeded_service):
        """recall() returns results for a query matching seeded data."""
        result = await recall(service=seeded_service, query="Ada Lovelace")

        assert result["count"] > 0
        assert result["query"] == "Ada Lovelace"

        # Should find at least one result mentioning Ada
        names_and_facts = []
        for r in result["results"]:
            if r["type"] == "edge":
                names_and_facts.append(r["fact"].lower())
            elif r["type"] == "node":
                names_and_facts.append(r["name"].lower())
        joined = " ".join(names_and_facts)
        assert "ada" in joined or "lovelace" in joined

    async def test_recall_returns_scores(self, seeded_service):
        """Each result has a numeric score."""
        result = await recall(service=seeded_service, query="computing")

        for r in result["results"]:
            assert isinstance(r["score"], (int, float))
            assert r["score"] >= 0

    async def test_recall_empty_query_still_works(self, seeded_service):
        """A query matching nothing returns empty results, not an error."""
        result = await recall(
            service=seeded_service,
            query="xyzzy plugh nothing matches this",
        )
        assert "error" not in result
        assert isinstance(result["results"], list)


# ---------------------------------------------------------------------------
# History tests
# ---------------------------------------------------------------------------

class TestLiveHistory:
    async def test_history_finds_entity_by_name(self, seeded_service):
        """history() finds Ada Lovelace and returns a timeline."""
        result = await history(service=seeded_service, entity_name="Ada Lovelace")

        # Should find the entity (might be named slightly differently by LLM)
        if result.get("error") == "not_found":
            for alt in ["Ada", "Lovelace", "Ada Lovelace"]:
                result = await history(service=seeded_service, entity_name=alt)
                if "entity" in result:
                    break

        assert "entity" in result, f"Could not find Ada Lovelace entity. Got: {result}"
        assert result["entity"]["name"]
        assert isinstance(result["timeline"], list)

    async def test_history_has_timeline_entries(self, seeded_service):
        """Timeline should have entries from seeded episodes."""
        result = await history(service=seeded_service, entity_name="Ada Lovelace")

        if result.get("error") == "not_found":
            for alt in ["Ada", "Lovelace"]:
                result = await history(service=seeded_service, entity_name=alt)
                if "entity" in result:
                    break

        if "entity" in result:
            assert result["count"] > 0, "Expected timeline entries from seeded episodes"
            entry = result["timeline"][0]
            assert "uuid" in entry
            assert "fact" in entry

    async def test_history_not_found(self, seeded_service):
        """Nonexistent entity returns not_found error."""
        result = await history(
            service=seeded_service,
            entity_name="Zaphod Beeblebrox",
        )
        assert result["error"] == "not_found"


# ---------------------------------------------------------------------------
# Inspect tests
# ---------------------------------------------------------------------------

class TestLiveInspect:
    async def test_inspect_entity_by_uuid(self, seeded_service):
        """inspect() returns full entity detail when given a node UUID."""
        search = await recall(service=seeded_service, query="Ada Lovelace")
        node_results = [r for r in search["results"] if r["type"] == "node"]

        if not node_results:
            pytest.skip("No entity nodes returned from recall — can't test inspect")

        node_uuid = node_results[0]["uuid"]
        result = await inspect(service=seeded_service, uuid=node_uuid)

        assert result["type"] == "entity"
        assert result["uuid"] == node_uuid
        assert result["name"]
        assert isinstance(result["edges"], list)
        assert isinstance(result["episodes"], list)

    async def test_inspect_edge_by_uuid(self, seeded_service):
        """inspect() returns full edge detail when given an edge UUID."""
        search = await recall(service=seeded_service, query="Ada Lovelace")
        edge_results = [r for r in search["results"] if r["type"] == "edge"]

        if not edge_results:
            pytest.skip("No edges returned from recall — can't test inspect")

        edge_uuid = edge_results[0]["uuid"]
        result = await inspect(service=seeded_service, uuid=edge_uuid)

        assert result["type"] == "edge"
        assert result["uuid"] == edge_uuid
        assert result["fact"]
        assert "source_entity" in result
        assert "target_entity" in result

    async def test_inspect_not_found(self, seeded_service):
        """Nonexistent UUID returns not_found error."""
        result = await inspect(
            service=seeded_service,
            uuid="00000000-0000-0000-0000-000000000000",
        )
        assert result["error"] == "not_found"


# ---------------------------------------------------------------------------
# Cross-tool integration
# ---------------------------------------------------------------------------

class TestLiveCrossTool:
    async def test_recall_then_inspect_roundtrip(self, seeded_service):
        """UUIDs from recall results can be passed to inspect."""
        search = await recall(service=seeded_service, query="Babbage")
        if search["count"] == 0:
            pytest.skip("No results for 'Babbage' — can't test roundtrip")

        first = search["results"][0]
        detail = await inspect(service=seeded_service, uuid=first["uuid"])

        assert detail.get("error") is None
        assert detail["uuid"] == first["uuid"]

    async def test_history_then_inspect_edge(self, seeded_service):
        """Edge UUIDs from history timeline can be inspected."""
        hist = await history(service=seeded_service, entity_name="Ada Lovelace")

        if hist.get("error") == "not_found":
            for alt in ["Ada", "Lovelace"]:
                hist = await history(service=seeded_service, entity_name=alt)
                if "entity" in hist:
                    break

        if hist.get("error") or not hist.get("timeline"):
            pytest.skip("No timeline entries to inspect")

        edge_uuid = hist["timeline"][0]["uuid"]
        detail = await inspect(service=seeded_service, uuid=edge_uuid)

        assert detail["type"] == "edge"
        assert detail["uuid"] == edge_uuid
