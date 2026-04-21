"""Tests for the persistent work queue.

TDD: these tests define the WorkQueue contract. All should fail
until queue.py is implemented.
"""

import asyncio
import json
from datetime import datetime
from uuid import UUID

import pytest

from helpers import wait_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _noop_handler(payload: dict) -> None:
    """Handler that succeeds immediately."""


async def _failing_handler(payload: dict) -> None:
    """Handler that always raises."""
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def queue(tmp_path):
    """A WorkQueue with a temp DB. Not started — tests register handlers first."""
    from pratyabhijna.queue import WorkQueue

    db_path = str(tmp_path / "test_queue.sqlite")
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
# Schema & init
# ---------------------------------------------------------------------------

class TestInit:
    async def test_start_creates_db(self, queue, tmp_path):
        db_file = tmp_path / "test_queue.sqlite"
        assert not db_file.exists()
        queue.register("test", _noop_handler)
        await queue.start()
        assert db_file.exists()

    async def test_start_is_idempotent(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        await queue.start()  # should not error


# ---------------------------------------------------------------------------
# Enqueueing
# ---------------------------------------------------------------------------

class TestEnqueue:
    async def test_enqueue_returns_uuid(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {"key": "value"})
        UUID(task_id)  # raises if not a valid UUID

    async def test_enqueue_creates_pending_task(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {"key": "value"})
        task = await queue.get_task(task_id)
        # Worker may claim the task before we check, so accept either state
        assert task["status"] in ("pending", "running", "completed")
        assert task["attempts"] <= 1

    async def test_enqueue_stores_payload(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        payload = {"nested": {"data": [1, 2, 3]}, "flag": True}
        task_id = await queue.enqueue("test", payload)
        task = await queue.get_task(task_id)
        assert json.loads(task["payload"]) == payload

    async def test_enqueue_unknown_type_raises(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        with pytest.raises(ValueError, match="no handler"):
            await queue.enqueue("unknown_type", {})

    async def test_enqueue_sets_timestamps(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})
        task = await queue.get_task(task_id)
        # Should parse as ISO timestamps without raising
        datetime.fromisoformat(task["created_at"])
        datetime.fromisoformat(task["updated_at"])


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

class TestProcessing:
    async def test_task_processes_to_completed(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        await wait_for(lambda: queue.get_task(task_id),
                        timeout=2.0)
        # Poll until completed
        async def is_done():
            t = await queue.get_task(task_id)
            return t and t["status"] == "completed"

        await wait_for(is_done)
        task = await queue.get_task(task_id)
        assert task["status"] == "completed"
        assert task["completed_at"] is not None

    async def test_handler_receives_payload(self, queue):
        received = []

        async def capture_handler(payload: dict) -> None:
            received.append(payload)

        queue.register("test", capture_handler)
        await queue.start()

        sent = {"message": "hello", "number": 42}
        task_id = await queue.enqueue("test", sent)

        async def is_done():
            t = await queue.get_task(task_id)
            return t and t["status"] == "completed"

        await wait_for(is_done)
        assert len(received) == 1
        assert received[0] == sent

    async def test_multiple_tasks_process_in_order(self, queue):
        order = []

        async def tracking_handler(payload: dict) -> None:
            order.append(payload["seq"])

        queue.register("test", tracking_handler)
        await queue.start()

        ids = []
        for i in range(3):
            tid = await queue.enqueue("test", {"seq": i})
            ids.append(tid)

        async def all_done():
            for tid in ids:
                t = await queue.get_task(tid)
                if not t or t["status"] != "completed":
                    return False
            return True

        await wait_for(all_done)
        assert order == [0, 1, 2]

    async def test_sequential_processing(self, queue):
        """Second task doesn't start until first finishes."""
        events = []
        gate = asyncio.Event()

        async def gated_handler(payload: dict) -> None:
            events.append(("start", payload["id"]))
            if payload.get("wait"):
                await gate.wait()
            events.append(("end", payload["id"]))

        queue.register("test", gated_handler)
        await queue.start()

        await queue.enqueue("test", {"id": "A", "wait": True})
        await queue.enqueue("test", {"id": "B", "wait": False})

        # Wait until task A has started
        await wait_for(lambda: ("start", "A") in events)
        # Task B should NOT have started yet
        assert ("start", "B") not in events

        gate.set()  # Release task A

        # Now wait for both to complete
        await wait_for(lambda: ("end", "B") in events)
        assert events == [("start", "A"), ("end", "A"),
                          ("start", "B"), ("end", "B")]


# ---------------------------------------------------------------------------
# Failure & retry
# ---------------------------------------------------------------------------

class TestFailureAndRetry:
    async def test_failed_task_retries(self, queue):
        call_count = 0

        async def fail_once(payload: dict) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")

        queue.register("test", fail_once)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_done():
            t = await queue.get_task(task_id)
            return t and t["status"] == "completed"

        await wait_for(is_done)
        assert call_count == 2

    async def test_retries_up_to_max(self, queue):
        call_count = 0

        async def always_fail(payload: dict) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always fails")

        queue.register("test", always_fail)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_dead():
            t = await queue.get_task(task_id)
            return t and t["status"] == "dead_letter"

        await wait_for(is_dead)
        assert call_count == 3  # max_retries=3

    async def test_dead_letter_after_max_attempts(self, queue):
        queue.register("test", _failing_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_dead():
            t = await queue.get_task(task_id)
            return t and t["status"] == "dead_letter"

        await wait_for(is_dead)
        task = await queue.get_task(task_id)
        assert task["status"] == "dead_letter"
        assert task["attempts"] == task["max_attempts"]
        assert task["completed_at"] is None  # dead letters didn't complete

    async def test_dead_letter_not_retried(self, queue):
        call_count = 0

        async def counting_fail(payload: dict) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("always fails")

        queue.register("test", counting_fail)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_dead():
            t = await queue.get_task(task_id)
            return t and t["status"] == "dead_letter"

        await wait_for(is_dead)
        before = call_count
        await asyncio.sleep(0.2)  # Give it time to wrongly retry
        assert call_count == before

    async def test_dead_letter_stores_error(self, queue):
        queue.register("test", _failing_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_dead():
            t = await queue.get_task(task_id)
            return t and t["status"] == "dead_letter"

        await wait_for(is_dead)
        task = await queue.get_task(task_id)
        assert "boom" in task["error"]

    async def test_dead_letters_returns_all(self, queue):
        queue.register("test", _failing_handler)
        await queue.start()

        id1 = await queue.enqueue("test", {"n": 1})
        id2 = await queue.enqueue("test", {"n": 2})

        async def both_dead():
            t1 = await queue.get_task(id1)
            t2 = await queue.get_task(id2)
            return (t1 and t1["status"] == "dead_letter" and
                    t2 and t2["status"] == "dead_letter")

        await wait_for(both_dead, timeout=5.0)
        dead = await queue.dead_letters()
        dead_ids = {d["id"] for d in dead}
        assert id1 in dead_ids
        assert id2 in dead_ids


# ---------------------------------------------------------------------------
# Persistence & recovery
# ---------------------------------------------------------------------------

class TestPersistence:
    async def test_task_survives_restart(self, tmp_path):
        from pratyabhijna.queue import WorkQueue

        db_path = str(tmp_path / "persist.sqlite")
        received = []

        async def capture(payload: dict) -> None:
            received.append(payload)

        # First instance: create schema only, then insert task directly
        # (if we use enqueue, the worker grabs it before stop)
        q1 = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
        q1.register("test", _noop_handler)
        await q1.start()
        await q1.stop()

        # Insert a pending task directly into the DB
        import aiosqlite
        from pratyabhijna.queue import _now
        task_id = "survive-test-id"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO tasks (id, task_type, payload, status, attempts,
                                      max_attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', 0, 3, ?, ?)""",
                (task_id, "test", json.dumps({"survived": True}), _now(), _now()),
            )
            await db.commit()

        # Second instance: should pick up the task
        q2 = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
        q2.register("test", capture)
        await q2.start()

        async def is_done():
            t = await q2.get_task(task_id)
            return t and t["status"] == "completed"

        await wait_for(is_done)
        assert received == [{"survived": True}]
        await q2.stop()

    async def _simulate_crash(self, db_path: str, task_id: str) -> None:
        """Force a task back to 'running' in the DB to simulate a mid-task crash."""
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,)
            )
            await db.commit()

    async def test_running_task_recovered_on_restart(self, tmp_path):
        """Crashed task is rescheduled with backoff and eventually completes."""
        from pratyabhijna.queue import WorkQueue

        db_path = str(tmp_path / "recover.sqlite")
        gate = asyncio.Event()
        started = asyncio.Event()

        async def blocking_handler(payload: dict) -> None:
            started.set()
            await gate.wait()

        q1 = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
        q1.register("test", blocking_handler)
        await q1.start()
        task_id = await q1.enqueue("test", {"recover": True})

        await asyncio.wait_for(started.wait(), timeout=2.0)
        gate.set()
        await q1.stop()
        await self._simulate_crash(db_path, task_id)

        recovered = []

        async def capture(payload: dict) -> None:
            recovered.append(payload)

        # Tiny backoff so the retry fires immediately in the test.
        q2 = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05, backoff_base_seconds=0.01)
        q2.register("test", capture)
        await q2.start()

        # After recovery: attempts=1, error set, run_at in the near past.
        t = await q2.get_task(task_id)
        assert t["status"] == "pending"
        assert t["attempts"] == 1
        assert "ServiceRestart" in t["error"]

        async def is_done():
            t = await q2.get_task(task_id)
            return t and t["status"] == "completed"

        await wait_for(is_done)
        assert recovered == [{"recover": True}]
        await q2.stop()

    async def test_running_task_dead_lettered_when_attempts_exhausted(self, tmp_path):
        """Crashed task is dead-lettered if it has no retries remaining."""
        from pratyabhijna.queue import WorkQueue

        db_path = str(tmp_path / "recover_dl.sqlite")

        async def always_succeed(payload: dict) -> None:
            pass

        q1 = WorkQueue(db_path=db_path, max_retries=1, poll_interval=0.05)
        q1.register("test", always_succeed)
        await q1.start()
        task_id = await q1.enqueue("test", {"data": "x"})
        await q1.stop()
        await self._simulate_crash(db_path, task_id)

        # Set attempts=0 so after +1 it equals max_retries=1 → dead-letter.
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE tasks SET attempts = 0 WHERE id = ?", (task_id,))
            await db.commit()

        q2 = WorkQueue(db_path=db_path, max_retries=1, poll_interval=0.05)
        q2.register("test", always_succeed)
        await q2.start()

        t = await q2.get_task(task_id)
        assert t["status"] == "dead_letter"
        assert t["attempts"] == 1
        assert "ServiceRestart" in t["error"]
        await q2.stop()

    async def test_recover_crashed_skipped_when_run_worker_false(self, tmp_path):
        """CLI-mode start (run_worker=False) must not touch running tasks."""
        from pratyabhijna.queue import WorkQueue

        db_path = str(tmp_path / "recover_cli.sqlite")

        async def noop(payload: dict) -> None:
            pass

        q1 = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
        q1.register("test", noop)
        await q1.start()
        task_id = await q1.enqueue("test", {})
        await q1.stop()
        await self._simulate_crash(db_path, task_id)

        # Open in CLI mode — running task must be left untouched.
        q2 = WorkQueue(db_path=db_path, max_retries=3, poll_interval=0.05)
        q2.register("test", noop)
        await q2.start(run_worker=False)

        t = await q2.get_task(task_id)
        assert t["status"] == "running"
        assert t["attempts"] == 0
        await q2.stop()


# ---------------------------------------------------------------------------
# Status queries
# ---------------------------------------------------------------------------

class TestStatusQueries:
    async def test_depth_zero_when_empty(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        assert await queue.depth() == 0

    async def test_depth_counts_pending_and_running(self, queue):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def blocking(payload: dict) -> None:
            started.set()
            await gate.wait()

        queue.register("test", blocking)
        await queue.start()

        id1 = await queue.enqueue("test", {"n": 1})
        id2 = await queue.enqueue("test", {"n": 2})

        # Wait for first task to start running
        await asyncio.wait_for(started.wait(), timeout=2.0)
        # One running, one pending → depth 2
        assert await queue.depth() == 2

        gate.set()  # Let tasks finish

        # Wait for both to complete
        async def both_done():
            t1 = await queue.get_task(id1)
            t2 = await queue.get_task(id2)
            return (t1["status"] == "completed" and t2["status"] == "completed")

        await wait_for(both_done)
        assert await queue.depth() == 0

    async def test_depth_excludes_completed_and_dead(self, queue):
        queue.register("good", _noop_handler)
        queue.register("bad", _failing_handler)
        await queue.start()

        good_id = await queue.enqueue("good", {})
        bad_id = await queue.enqueue("bad", {})

        async def both_done():
            g = await queue.get_task(good_id)
            b = await queue.get_task(bad_id)
            return (g and g["status"] == "completed" and
                    b and b["status"] == "dead_letter")

        await wait_for(both_done, timeout=5.0)
        assert await queue.depth() == 0

    async def test_last_write_none_when_empty(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        assert await queue.last_write() is None

    async def test_last_write_returns_latest(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()

        id1 = await queue.enqueue("test", {"n": 1})
        async def first_done():
            t = await queue.get_task(id1)
            return t and t["status"] == "completed"
        await wait_for(first_done)

        id2 = await queue.enqueue("test", {"n": 2})
        async def second_done():
            t = await queue.get_task(id2)
            return t and t["status"] == "completed"
        await wait_for(second_done)

        last = await queue.last_write()
        assert last is not None
        t2 = await queue.get_task(id2)
        assert last == t2["completed_at"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_stop_waits_for_current_task(self, queue):
        completed = asyncio.Event()

        async def slow_handler(payload: dict) -> None:
            await asyncio.sleep(0.2)
            completed.set()

        queue.register("test", slow_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        # Wait for the task to start processing
        async def is_running():
            t = await queue.get_task(task_id)
            return t and t["status"] == "running"

        await wait_for(is_running)

        # Stop should wait for the task to finish
        await queue.stop()
        assert completed.is_set()

        # Verify via a fresh connection that the task was completed
        import aiosqlite
        async with aiosqlite.connect(queue.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)) as cur:
                row = await cur.fetchone()
                assert row["status"] == "completed"

    async def test_stop_then_start(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        await queue.stop()

        # Restart
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_done():
            t = await queue.get_task(task_id)
            return t and t["status"] == "completed"

        await wait_for(is_done)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TestLogging:
    async def test_warning_logged_on_retry(self, queue, caplog):
        """A WARNING is emitted when a task fails but has retries remaining."""
        import logging

        call_count = 0

        async def fail_once(payload: dict) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")

        queue.register("test", fail_once)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_done():
            t = await queue.get_task(task_id)
            return t and t["status"] == "completed"

        with caplog.at_level(logging.WARNING, logger="pratyabhijna.queue"):
            await wait_for(is_done)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(task_id in r.message for r in warning_records)
        assert any("first attempt fails" in r.message for r in warning_records)

    async def test_error_logged_on_dead_letter(self, queue, caplog):
        """An ERROR is emitted when a task is dead-lettered."""
        import logging

        queue.register("test", _failing_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_dead():
            t = await queue.get_task(task_id)
            return t and t["status"] == "dead_letter"

        with caplog.at_level(logging.ERROR, logger="pratyabhijna.queue"):
            await wait_for(is_dead)

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any(task_id in r.message for r in error_records)
        assert any("dead-lettered" in r.message for r in error_records)

    async def test_debug_logged_on_completion(self, queue, caplog):
        """A DEBUG message is emitted when a task completes successfully."""
        import logging

        queue.register("test", _noop_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_done():
            t = await queue.get_task(task_id)
            return t and t["status"] == "completed"

        with caplog.at_level(logging.DEBUG, logger="pratyabhijna.queue"):
            await wait_for(is_done)

        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(task_id in r.message and "completed" in r.message for r in debug_records)


# ---------------------------------------------------------------------------
# last_error query
# ---------------------------------------------------------------------------

class TestLastError:
    async def test_last_error_none_when_no_dead_letters(self, queue):
        queue.register("test", _noop_handler)
        await queue.start()
        assert await queue.last_error() is None

    async def test_last_error_returns_most_recent(self, queue):
        queue.register("test", _failing_handler)
        await queue.start()
        task_id = await queue.enqueue("test", {})

        async def is_dead():
            t = await queue.get_task(task_id)
            return t and t["status"] == "dead_letter"

        await wait_for(is_dead)
        err = await queue.last_error()
        assert err is not None
        assert err["task_id"] == task_id
        assert "boom" in err["error"]
        assert "updated_at" in err


# ---------------------------------------------------------------------------
# collect_queue_stats — unified read path for status() (MCP + CLI)
# ---------------------------------------------------------------------------

class TestCollectQueueStats:
    """`collect_queue_stats(db_path)` reads queue counters from SQLite directly.

    Used by both the MCP status tool and the CLI status subcommand so they
    surface the same information without instantiating a live WorkQueue
    (which would clobber the running server's worker).
    """

    def test_missing_db_returns_zero_state(self, tmp_path):
        """A nonexistent DB path returns zeros and empty breakdown."""
        from pratyabhijna.queue import collect_queue_stats

        stats = collect_queue_stats(str(tmp_path / "does_not_exist.sqlite"))
        assert stats["depth"] == 0
        assert stats["last_write"] is None
        assert stats["dead_letters"] == 0
        assert stats["last_error"] is None
        assert stats["by_task_type"] == {}

    async def test_empty_db_returns_zero_state(self, tmp_path):
        """A DB with schema but no rows returns zeros and empty breakdown."""
        from pratyabhijna.queue import WorkQueue, collect_queue_stats

        db_path = str(tmp_path / "empty.sqlite")
        q = WorkQueue(db_path=db_path, poll_interval=0.05)
        q.register("noop", _noop_handler)
        await q.start()
        await q.stop()

        stats = collect_queue_stats(db_path)
        assert stats["depth"] == 0
        assert stats["last_write"] is None
        assert stats["dead_letters"] == 0
        assert stats["last_error"] is None
        assert stats["by_task_type"] == {}

    async def test_counts_completed_by_task_type(self, tmp_path):
        """`by_task_type` splits counts per task type and status."""
        from pratyabhijna.queue import WorkQueue, collect_queue_stats

        db_path = str(tmp_path / "by_type.sqlite")
        q = WorkQueue(db_path=db_path, poll_interval=0.05)
        q.register("add_episode", _noop_handler)
        q.register("correct_memory", _noop_handler)
        await q.start()

        await q.enqueue("add_episode", {})
        await q.enqueue("add_episode", {})
        await q.enqueue("add_episode", {})
        await q.enqueue("correct_memory", {})

        async def all_done():
            return (await q.depth()) == 0

        await wait_for(all_done)
        await q.stop()

        stats = collect_queue_stats(db_path)
        assert stats["by_task_type"]["add_episode"]["completed"] == 3
        assert stats["by_task_type"]["correct_memory"]["completed"] == 1
        assert stats["by_task_type"]["add_episode"].get("dead_letter", 0) == 0

    async def test_counts_dead_letters_by_task_type(self, tmp_path):
        """Dead-lettered tasks show up under their task type."""
        from pratyabhijna.queue import WorkQueue, collect_queue_stats

        db_path = str(tmp_path / "dead_by_type.sqlite")
        q = WorkQueue(
            db_path=db_path,
            max_retries=1,
            poll_interval=0.05,
            backoff_base_seconds=0.01,
        )
        q.register("correct_memory", _failing_handler)
        await q.start()

        task_id = await q.enqueue("correct_memory", {})

        async def is_dead():
            t = await q.get_task(task_id)
            return t and t["status"] == "dead_letter"

        await wait_for(is_dead)
        await q.stop()

        stats = collect_queue_stats(db_path)
        assert stats["by_task_type"]["correct_memory"]["dead_letter"] == 1
        assert stats["dead_letters"] == 1

    async def test_matches_existing_queue_methods(self, tmp_path):
        """`collect_queue_stats` returns the same top-level values as live WorkQueue methods."""
        from pratyabhijna.queue import WorkQueue, collect_queue_stats

        db_path = str(tmp_path / "parity.sqlite")
        q = WorkQueue(db_path=db_path, poll_interval=0.05)
        q.register("add_episode", _noop_handler)
        await q.start()

        await q.enqueue("add_episode", {})

        async def all_done():
            return (await q.depth()) == 0

        await wait_for(all_done)

        live_depth = await q.depth()
        live_last_write = await q.last_write()
        live_dead = len(await q.dead_letters())
        live_err = await q.last_error()
        await q.stop()

        stats = collect_queue_stats(db_path)
        assert stats["depth"] == live_depth
        assert stats["last_write"] == live_last_write
        assert stats["dead_letters"] == live_dead
        assert stats["last_error"] == live_err


# ---------------------------------------------------------------------------
# Transaction hygiene
# ---------------------------------------------------------------------------


class TestClaimNextTransactionHygiene:
    async def test_claim_next_does_not_leave_open_transaction_when_idle(self, tmp_path):
        """_claim_next must not hold an open SQLite transaction when the queue is empty.

        Root cause of 'database is locked': the UPDATE in _claim_next begins an
        implicit transaction even when rowcount==0. Without a rollback (or commit),
        that transaction holds a RESERVED lock for the full poll interval, blocking
        the CLI's INSERT in enqueue().
        """
        from pratyabhijna.queue import WorkQueue

        db_path = str(tmp_path / "tx_hygiene.sqlite")
        q = WorkQueue(db_path=db_path, poll_interval=0.5)
        q.register("add_episode", _noop_handler)
        await q.start(run_worker=False)

        result = await q._claim_next()
        assert result is None
        assert not q._db.in_transaction

        await q.stop()
