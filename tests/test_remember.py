"""Tests for the `remember` MCP tool and its queue handler.

TDD: these tests define the remember tool contract. They should fail
until remember.py is implemented and wired into the server.

The remember tool enqueues an add_episode task and returns immediately.
The handler calls graphiti.add_episode() with the content and entity types.
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
    """A mock PratyabhijnaService with a mock graphiti client.

    use_in_house defaults to False to match production config; tests that
    exercise the in-house path explicitly set it on the fixture.
    """
    service = MagicMock()
    service.is_connected = True
    service._graphiti = AsyncMock()
    service._graphiti.add_episode = AsyncMock()
    service.config.add_episode.use_in_house = False
    service.entity_types = {
        "Person": MagicMock(),
        "Observation": MagicMock(),
    }
    return service


@pytest.fixture
async def wired_queue(tmp_path, mock_service):
    """A real WorkQueue with remember handler registered and started."""
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.tools.remember import make_handler

    db_path = str(tmp_path / "test_remember.sqlite")
    q = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
    handler = make_handler(mock_service)
    q.register("add_episode", handler)
    await q.start()
    yield q
    await q.stop()


# ---------------------------------------------------------------------------
# Tool return value
# ---------------------------------------------------------------------------

class TestRememberReturn:
    async def test_remember_returns_task_id(self, wired_queue):
        """remember() returns a dict with a valid UUID task_id and status 'queued'."""
        from pratyabhijna.tools.remember import remember

        result = await remember(
            queue=wired_queue,
            content="Serah prefers Ruby for scripting.",
        )
        assert result["status"] == "queued"
        UUID(result["task_id"])  # raises if not valid UUID

    async def test_remember_default_memory_type(self, wired_queue):
        """memory_type defaults to 'observation'."""
        from pratyabhijna.tools.remember import remember

        result = await remember(queue=wired_queue, content="test")
        task = await wired_queue.get_task(result["task_id"])
        payload = json.loads(task["payload"])
        assert payload["memory_type"] == "observation"

    async def test_remember_default_source(self, wired_queue):
        """source defaults to 'self' — the subject remembering about itself."""
        from pratyabhijna.tools.remember import remember

        result = await remember(queue=wired_queue, content="test")
        task = await wired_queue.get_task(result["task_id"])
        payload = json.loads(task["payload"])
        assert payload["source"] == "self"


# ---------------------------------------------------------------------------
# Enqueueing
# ---------------------------------------------------------------------------

class TestRememberEnqueue:
    async def test_remember_enqueues_add_episode_task(self, wired_queue):
        """remember() creates a task with type 'add_episode'."""
        from pratyabhijna.tools.remember import remember

        result = await remember(queue=wired_queue, content="test content")
        task = await wired_queue.get_task(result["task_id"])
        assert task["task_type"] == "add_episode"
        assert task["status"] == "pending" or task["status"] == "running"

    async def test_remember_payload_includes_content_and_type(self, wired_queue):
        """Task payload contains content, memory_type, and source."""
        from pratyabhijna.tools.remember import remember

        result = await remember(
            queue=wired_queue,
            content="Vesper orients toward thresholds.",
            memory_type="identity",
            source="serah",
        )
        task = await wired_queue.get_task(result["task_id"])
        payload = json.loads(task["payload"])
        assert payload["content"] == "Vesper orients toward thresholds."
        assert payload["memory_type"] == "identity"
        assert payload["source"] == "serah"

    async def test_remember_does_not_block(self, wired_queue, mock_service):
        """remember() returns before the handler finishes processing."""
        processing = asyncio.Event()
        released = asyncio.Event()

        async def slow_add_episode(**kwargs):
            processing.set()
            await released.wait()

        mock_service._graphiti.add_episode = slow_add_episode

        from pratyabhijna.tools.remember import remember

        result = await remember(queue=wired_queue, content="test")
        # We got a result back — the tool returned
        assert result["status"] == "queued"
        # Wait for handler to start processing, proving it's async
        await asyncio.wait_for(processing.wait(), timeout=2.0)
        # Release the handler so cleanup can proceed
        released.set()


# ---------------------------------------------------------------------------
# Handler processing
# ---------------------------------------------------------------------------

class TestRememberHandler:
    async def test_handler_calls_add_episode(self, wired_queue, mock_service):
        """After processing, graphiti.add_episode is called with content and entity_types."""
        from pratyabhijna.tools.remember import remember

        await remember(
            queue=wired_queue,
            content="Serah values directness.",
            memory_type="observation",
            source="vesper",
        )

        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        call_kwargs = mock_service._graphiti.add_episode.call_args
        # Content should be passed through
        assert "Serah values directness." in str(call_kwargs)

    async def test_handler_passes_entity_types(self, wired_queue, mock_service):
        """Handler passes entity_types from the service to add_episode."""
        from pratyabhijna.tools.remember import remember

        await remember(queue=wired_queue, content="test")
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        call_kwargs = mock_service._graphiti.add_episode.call_args
        # entity_types should be passed through
        assert "entity_types" in str(call_kwargs)

    async def test_handler_uses_now_when_occurred_at_absent(
        self, wired_queue, mock_service
    ):
        """Without occurred_at, reference_time defaults to ~now (UTC)."""
        from datetime import datetime, timezone

        from pratyabhijna.tools.remember import remember

        before = datetime.now(timezone.utc)
        await remember(queue=wired_queue, content="test")
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        after = datetime.now(timezone.utc)

        ref = mock_service._graphiti.add_episode.call_args.kwargs["reference_time"]
        assert before <= ref <= after

    async def test_handler_uses_occurred_at_when_provided(
        self, wired_queue, mock_service
    ):
        """occurred_at (ISO-8601) becomes the add_episode reference_time."""
        from datetime import datetime, timezone

        from pratyabhijna.tools.remember import remember

        await remember(
            queue=wired_queue,
            content="Serah adopted Beth-Ella last week.",
            occurred_at="2026-04-06T14:30:00+00:00",
        )
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        ref = mock_service._graphiti.add_episode.call_args.kwargs["reference_time"]
        assert ref == datetime(2026, 4, 6, 14, 30, tzinfo=timezone.utc)

    async def test_handler_accepts_trailing_z(self, wired_queue, mock_service):
        """ISO-8601 with trailing 'Z' parses as UTC."""
        from datetime import datetime, timezone

        from pratyabhijna.tools.remember import remember

        await remember(
            queue=wired_queue,
            content="event",
            occurred_at="2026-01-15T09:00:00Z",
        )
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        ref = mock_service._graphiti.add_episode.call_args.kwargs["reference_time"]
        assert ref == datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)

    async def test_handler_passes_source(self, wired_queue, mock_service):
        """Source attribution is included in the add_episode call."""
        from pratyabhijna.tools.remember import remember

        await remember(
            queue=wired_queue,
            content="test content",
            source="serah",
        )
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        call_kwargs = mock_service._graphiti.add_episode.call_args
        assert "serah" in str(call_kwargs)

    async def test_handler_passes_saga(self, wired_queue, mock_service):
        """saga is forwarded to add_episode when provided."""
        from pratyabhijna.tools.remember import remember

        await remember(
            queue=wired_queue,
            content="solo session 1 content",
            saga="solo-sessions",
        )
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        kwargs = mock_service._graphiti.add_episode.call_args.kwargs
        assert kwargs["saga"] == "solo-sessions"
        assert kwargs["saga_previous_episode_uuid"] is None

    async def test_handler_passes_saga_previous_episode_uuid(self, wired_queue, mock_service):
        """saga_previous_episode_uuid is forwarded when provided."""
        from pratyabhijna.tools.remember import remember

        prev_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        await remember(
            queue=wired_queue,
            content="solo session 2 content",
            saga="solo-sessions",
            saga_previous_episode_uuid=prev_uuid,
        )
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        kwargs = mock_service._graphiti.add_episode.call_args.kwargs
        assert kwargs["saga"] == "solo-sessions"
        assert kwargs["saga_previous_episode_uuid"] == prev_uuid

    async def test_handler_omits_saga_when_absent(self, wired_queue, mock_service):
        """saga and saga_previous_episode_uuid default to None when not passed."""
        from pratyabhijna.tools.remember import remember

        await remember(queue=wired_queue, content="ordinary memory")
        await wait_for(lambda: mock_service._graphiti.add_episode.called)
        kwargs = mock_service._graphiti.add_episode.call_args.kwargs
        assert kwargs["saga"] is None
        assert kwargs["saga_previous_episode_uuid"] is None


# ---------------------------------------------------------------------------
# Feature flag — in-house dispatch
# ---------------------------------------------------------------------------


class TestInHouseDispatch:
    """When config.add_episode.use_in_house is True, dispatch to pratyabhijna.add_episode."""

    async def test_remember_calls_in_house_when_flag_on(self, tmp_path, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock
        from pratyabhijna.queue import WorkQueue
        from pratyabhijna.tools.remember import make_handler, remember

        service = MagicMock()
        service._graphiti = MagicMock()
        service._graphiti.add_episode = AsyncMock()
        service.config.add_episode.use_in_house = True
        service.config.subject_name = "Vesper"
        service.entity_types = {}

        in_house = AsyncMock()
        monkeypatch.setattr("pratyabhijna.add_episode.add_episode", in_house)

        db_path = str(tmp_path / "test_inhouse.sqlite")
        q = WorkQueue(db_path=db_path, max_retries=1, poll_interval=0.05)
        q.register("add_episode", make_handler(service))
        await q.start()
        try:
            await remember(queue=q, content="test")
            await wait_for(lambda: in_house.called)
        finally:
            await q.stop()

        in_house.assert_awaited_once()
        # Graphiti's add_episode was NOT called.
        service._graphiti.add_episode.assert_not_called()
