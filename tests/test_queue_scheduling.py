"""Tests for work queue scheduling: run_at and singleton tasks.

TDD: these tests define the scheduling contract. They should fail
until the queue is extended with run_at column support and the
reschedule_or_enqueue method.

The scheduling feature supports write-triggered synthesis rebuilds:
identity-relevant writes enqueue a singleton rebuild task with a
future run_at, and subsequent writes kick that time forward.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from pratyabhijna.queue import WorkQueue


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _noop_handler(payload: dict) -> None:
    """Handler that does nothing — used when we only care about scheduling."""
    pass


@pytest.fixture
async def queue(tmp_path):
    """A WorkQueue with temp DB. Not started — tests register handlers first."""
    db_path = str(tmp_path / "test_scheduling.sqlite")
    q = WorkQueue(
        db_path=db_path,
        max_retries=3,
        poll_interval=0.05,
        backoff_base_seconds=0.01,
    )
    yield q
    if q.is_running:
        await q.stop()


# ---------------------------------------------------------------------------
# run_at column behavior
# ---------------------------------------------------------------------------

class TestRunAtColumn:
    async def test_enqueue_without_run_at_is_immediately_claimable(self, queue):
        """Backward compat: enqueue() with no run_at processes immediately."""
        processed = []

        async def capture(payload):
            processed.append(payload)

        queue.register("test_task", capture)
        await queue.start()
        await queue.enqueue("test_task", {"key": "value"})

        # Give worker time to process
        await asyncio.sleep(0.2)
        assert len(processed) == 1
        assert processed[0]["key"] == "value"

    async def test_enqueue_with_future_run_at_not_claimed_yet(self, queue):
        """A task with run_at in the future is not processed immediately."""
        processed = []

        async def capture(payload):
            processed.append(payload)

        queue.register("test_task", capture)
        await queue.start()

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        await queue.enqueue("test_task", {"key": "deferred"}, run_at=future)

        # Worker should skip it
        await asyncio.sleep(0.2)
        assert len(processed) == 0

        # But it's still in the queue
        depth = await queue.depth()
        assert depth == 1

    async def test_task_claimed_after_run_at_passes(self, queue):
        """A task with a near-future run_at is processed once the time arrives."""
        processed = []

        async def capture(payload):
            processed.append(payload)

        queue.register("test_task", capture)
        await queue.start()

        soon = datetime.now(timezone.utc) + timedelta(seconds=0.1)
        await queue.enqueue("test_task", {"key": "soon"}, run_at=soon)

        # Wait for run_at to pass and worker to pick it up
        await asyncio.sleep(0.4)
        assert len(processed) == 1

    async def test_run_at_null_treated_as_immediate(self, queue):
        """Explicit run_at=None behaves the same as omitting it."""
        processed = []

        async def capture(payload):
            processed.append(payload)

        queue.register("test_task", capture)
        await queue.start()
        await queue.enqueue("test_task", {"key": "now"}, run_at=None)

        await asyncio.sleep(0.2)
        assert len(processed) == 1

    async def test_run_at_in_past_is_immediately_claimable(self, queue):
        """A task with run_at in the past processes immediately."""
        processed = []

        async def capture(payload):
            processed.append(payload)

        queue.register("test_task", capture)
        await queue.start()

        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await queue.enqueue("test_task", {"key": "overdue"}, run_at=past)

        await asyncio.sleep(0.2)
        assert len(processed) == 1


# ---------------------------------------------------------------------------
# Singleton scheduling: reschedule_or_enqueue
# ---------------------------------------------------------------------------

class TestRescheduleOrEnqueue:
    async def test_creates_task_when_none_pending(self, queue):
        """reschedule_or_enqueue creates a new task when none exists."""
        queue.register("synthesize", _noop_handler)
        await queue.start()

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        task_id = await queue.reschedule_or_enqueue("synthesize", {}, run_at=future)

        assert task_id is not None
        task = await queue.get_task(task_id)
        assert task is not None
        assert task["status"] == "pending"

    async def test_updates_run_at_when_pending_exists(self, queue):
        """Second call updates the existing task's run_at instead of creating new."""
        queue.register("synthesize", _noop_handler)
        await queue.start()

        t1 = datetime.now(timezone.utc) + timedelta(hours=1)
        t2 = datetime.now(timezone.utc) + timedelta(hours=2)

        id1 = await queue.reschedule_or_enqueue("synthesize", {}, run_at=t1)
        id2 = await queue.reschedule_or_enqueue("synthesize", {}, run_at=t2)

        # Same task, updated
        assert id1 == id2

    async def test_does_not_create_duplicate(self, queue):
        """Multiple calls produce exactly one pending task of that type."""
        queue.register("synthesize", _noop_handler)
        await queue.start()

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        for _ in range(5):
            await queue.reschedule_or_enqueue("synthesize", {}, run_at=future)

        depth = await queue.depth()
        assert depth == 1

    async def test_kicks_run_at_forward(self, queue):
        """Each call pushes run_at to the new (later) time."""
        queue.register("synthesize", _noop_handler)
        await queue.start()

        t1 = datetime.now(timezone.utc) + timedelta(hours=1)
        t2 = datetime.now(timezone.utc) + timedelta(hours=2)

        task_id = await queue.reschedule_or_enqueue("synthesize", {}, run_at=t1)
        await queue.reschedule_or_enqueue("synthesize", {}, run_at=t2)

        task = await queue.get_task(task_id)
        stored_run_at = datetime.fromisoformat(task["run_at"])
        assert abs(stored_run_at - t2) < timedelta(seconds=1)

    async def test_does_not_affect_running_task(self, queue):
        """If a task of that type is running, a new pending one is created."""
        hold = asyncio.Event()

        async def slow_handler(payload):
            await hold.wait()

        queue.register("synthesize", slow_handler)
        await queue.start()

        # Enqueue a task that will start running immediately
        await queue.enqueue("synthesize", {})
        await asyncio.sleep(0.2)  # Let worker claim it

        # Now reschedule — should create a second task since first is running
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        task_id = await queue.reschedule_or_enqueue("synthesize", {}, run_at=future)

        task = await queue.get_task(task_id)
        assert task["status"] == "pending"

        # Clean up
        hold.set()
        await asyncio.sleep(0.2)

    async def test_does_not_affect_completed_task(self, queue):
        """Completed tasks don't prevent creating a new pending one."""
        queue.register("synthesize", _noop_handler)
        await queue.start()

        # Let one complete
        await queue.enqueue("synthesize", {})
        await asyncio.sleep(0.2)

        # Now reschedule — should create a new pending task
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        task_id = await queue.reschedule_or_enqueue("synthesize", {}, run_at=future)

        task = await queue.get_task(task_id)
        assert task["status"] == "pending"

    async def test_returns_task_id(self, queue):
        """reschedule_or_enqueue always returns a task ID string."""
        queue.register("synthesize", _noop_handler)
        await queue.start()

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        task_id = await queue.reschedule_or_enqueue("synthesize", {}, run_at=future)

        assert isinstance(task_id, str)
        assert len(task_id) > 0

    async def test_requires_registered_handler(self, queue):
        """reschedule_or_enqueue raises ValueError without a registered handler."""
        await queue.start()

        future = datetime.now(timezone.utc) + timedelta(hours=2)
        with pytest.raises(ValueError, match="no handler registered"):
            await queue.reschedule_or_enqueue("unknown_type", {}, run_at=future)
