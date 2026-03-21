"""Tests for the `correct` MCP tool and its queue handler.

TDD: these tests define the correct tool contract. They should fail
until correct.py is implemented and wired into the server.

The correct tool enqueues a correction task and returns immediately.
The handler stores the correction as an episode — Graphiti handles
edge invalidation internally via its bi-temporal model.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from helpers import wait_for


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """A mock PratyabhijnaService with a mock graphiti client."""
    service = MagicMock()
    service.is_connected = True
    service._graphiti = AsyncMock()
    service._graphiti.add_episode = AsyncMock()
    service.entity_types = {
        "Person": MagicMock(),
        "Observation": MagicMock(),
    }
    return service


@pytest.fixture
async def wired_queue(tmp_path, mock_service):
    """A real WorkQueue with correct handler registered and started."""
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.tools.correct import make_handler

    db_path = str(tmp_path / "test_correct.sqlite")
    q = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
    handler = make_handler(mock_service)
    q.register("correct_memory", handler)
    await q.start()
    yield q
    await q.stop()


# ---------------------------------------------------------------------------
# Tool return value
# ---------------------------------------------------------------------------

class TestCorrectReturn:
    async def test_correct_returns_task_id(self, wired_queue):
        """correct() returns a dict with a valid UUID task_id and status 'queued'."""
        from pratyabhijna.tools.correct import correct

        result = await correct(
            queue=wired_queue,
            content="Vesper's pronouns are it/its, not they/them.",
            search_terms="Vesper pronouns",
        )
        assert result["status"] == "queued"
        UUID(result["task_id"])  # raises if not valid UUID


# ---------------------------------------------------------------------------
# Enqueueing
# ---------------------------------------------------------------------------

class TestCorrectEnqueue:
    async def test_correct_enqueues_correction_task(self, wired_queue):
        """correct() creates a task with type 'correct_memory'."""
        from pratyabhijna.tools.correct import correct

        result = await correct(
            queue=wired_queue,
            content="Kuzu is deprecated, not active.",
            search_terms="Kuzu database status",
        )
        task = await wired_queue.get_task(result["task_id"])
        assert task["task_type"] == "correct_memory"

    async def test_correct_payload_includes_content_and_search_terms(self, wired_queue):
        """Task payload contains content and search_terms."""
        from pratyabhijna.tools.correct import correct

        result = await correct(
            queue=wired_queue,
            content="The graph DB is Neo4j, not Kuzu.",
            search_terms="graph database backend",
        )
        task = await wired_queue.get_task(result["task_id"])
        payload = json.loads(task["payload"])
        assert payload["content"] == "The graph DB is Neo4j, not Kuzu."
        assert payload["search_terms"] == "graph database backend"

    async def test_correct_does_not_block(self, wired_queue, mock_service):
        """correct() returns before the handler finishes processing."""
        processing = asyncio.Event()
        released = asyncio.Event()

        async def slow_add_episode(**kwargs):
            processing.set()
            await released.wait()

        mock_service._graphiti.add_episode = slow_add_episode

        from pratyabhijna.tools.correct import correct

        result = await correct(
            queue=wired_queue,
            content="correction",
            search_terms="something",
        )
        assert result["status"] == "queued"
        await asyncio.wait_for(processing.wait(), timeout=2.0)
        released.set()


# ---------------------------------------------------------------------------
# Handler processing
# ---------------------------------------------------------------------------

class TestCorrectHandler:
    async def test_handler_calls_add_episode(self, wired_queue, mock_service):
        """After processing, graphiti.add_episode is called with correction content."""
        from pratyabhijna.tools.correct import correct

        await correct(
            queue=wired_queue,
            content="Serah's pronouns are she/her, not they/them.",
            search_terms="Serah pronouns",
        )

        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        call_kwargs = mock_service._graphiti.add_episode.call_args
        # Correction content should be passed through
        assert "Serah's pronouns are she/her" in str(call_kwargs)
