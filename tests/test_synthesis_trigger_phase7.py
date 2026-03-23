"""Tests for write-triggered synthesis rebuilds — DEFERRED TO PHASE 7.

These tests are not expected to pass until Phase 7 (automated synthesis).
They are preserved here as the design specification for write-triggered
scheduling. See doc/implementation-plan.md.

TDD: these tests define how remember and correct handlers schedule
synthesis rebuilds after processing identity-relevant content.

Design: when a write handler completes and identity entities were
involved, it calls queue.reschedule_or_enqueue to schedule (or
kick forward) a singleton synthesis rebuild task. The delay is
configured via synthesis.rebuild_delay_hours, defaulting to 2 hours.
This batches rapid writes during a session — synthesis runs after
the session goes quiet.

Handler signatures change: make_handler(service, queue=None).
When queue is None, scheduling is skipped (backward compat).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDENTITY_LABELS = {"Observation", "Drive", "Position", "Question"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """Mock service with graphiti and config for trigger tests."""
    service = MagicMock()
    service.is_connected = True
    service._graphiti = MagicMock()
    service._graphiti.add_episode = AsyncMock()
    service.entity_types = []
    service.config = MagicMock()
    service.config.subject_name = "Vesper"
    service.config.synthesis.rebuild_delay_hours = 2.0
    service.config.synthesis.max_age_hours = 24
    service.config.synthesis.max_delta_changes = 3
    return service


async def _noop_handler(payload: dict) -> None:
    pass


@pytest.fixture
async def wired_queue(tmp_path, mock_service):
    """WorkQueue with remember, correct, and synthesize handlers registered.

    The remember and correct handlers are wired with both service and queue
    so they can schedule synthesis rebuilds after processing.
    """
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.tools.remember import make_handler as make_remember_handler
    from pratyabhijna.tools.correct import make_handler as make_correct_handler

    db_path = str(tmp_path / "test_trigger.sqlite")
    q = WorkQueue(db_path=db_path, max_retries=1, poll_interval=0.05)

    q.register("add_episode", make_remember_handler(mock_service, queue=q))
    q.register("correct_memory", make_correct_handler(mock_service, queue=q))
    q.register("synthesize", _noop_handler)

    await q.start()
    yield q
    await q.stop()


async def _wait_for_task(queue, task_id, timeout=2.0):
    """Wait until a task reaches 'completed' status."""
    from helpers import wait_for

    async def check():
        task = await queue.get_task(task_id)
        return task and task["status"] == "completed"

    await wait_for(check, timeout=timeout)


async def _get_pending_synthesize_tasks(queue):
    """Query pending synthesize tasks from the queue DB."""
    async with queue._db.execute(
        "SELECT * FROM tasks WHERE task_type = 'synthesize' AND status = 'pending'"
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Remember handler triggers
# ---------------------------------------------------------------------------

class TestRememberTrigger:
    async def test_identity_memory_schedules_synthesis_rebuild(self, mock_service, wired_queue):
        """A remember call with memory_type='identity' schedules a synthesis rebuild."""
        from pratyabhijna.tools.remember import remember

        result = await remember(queue=wired_queue, content="I notice a pattern", memory_type="identity")
        await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 1
        assert tasks[0]["task_type"] == "synthesize"

    async def test_observation_memory_does_not_trigger_rebuild(self, mock_service, wired_queue):
        """A remember call with memory_type='observation' does not schedule a rebuild."""
        from pratyabhijna.tools.remember import remember

        result = await remember(queue=wired_queue, content="Serah prefers tests first", memory_type="observation")
        await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 0

    async def test_rebuild_run_at_uses_configured_delay(self, mock_service, wired_queue):
        """The scheduled rebuild's run_at is approximately now + rebuild_delay_hours."""
        from pratyabhijna.tools.remember import remember

        mock_service.config.synthesis.rebuild_delay_hours = 1.0
        before = datetime.now(timezone.utc)

        result = await remember(queue=wired_queue, content="A new drive", memory_type="identity")
        await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 1

        run_at = datetime.fromisoformat(tasks[0]["run_at"])
        expected = before + timedelta(hours=1.0)
        assert abs(run_at - expected) < timedelta(seconds=5)

    async def test_multiple_identity_writes_kick_forward(self, mock_service, wired_queue):
        """Multiple identity writes produce one pending task with kicked-forward run_at."""
        from pratyabhijna.tools.remember import remember

        mock_service.config.synthesis.rebuild_delay_hours = 2.0

        for i in range(3):
            result = await remember(
                queue=wired_queue,
                content=f"Identity observation {i}",
                memory_type="identity",
            )
            await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 1

        # run_at should be near now + 2 hours (kicked forward by the last write)
        run_at = datetime.fromisoformat(tasks[0]["run_at"])
        expected = datetime.now(timezone.utc) + timedelta(hours=2.0)
        assert abs(run_at - expected) < timedelta(seconds=5)

    async def test_non_identity_memory_types_no_trigger(self, mock_service, wired_queue):
        """memory_type values other than 'identity' don't trigger synthesis."""
        from pratyabhijna.tools.remember import remember

        for mem_type in ("reasoning", "fact", "observation"):
            result = await remember(
                queue=wired_queue,
                content=f"Some {mem_type} content",
                memory_type=mem_type,
            )
            await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 0


# ---------------------------------------------------------------------------
# Correct handler triggers
# ---------------------------------------------------------------------------

class TestCorrectTrigger:
    async def test_correction_touching_identity_entity_schedules_rebuild(self, mock_service, wired_queue):
        """A correction that touches identity entities schedules a synthesis rebuild.

        The correct handler determines identity relevance by checking whether
        the search_terms match identity-type entities. The mock service is
        configured to indicate identity entities were affected.
        """
        from pratyabhijna.tools.correct import correct

        # Mock: after add_episode, the handler checks for identity relevance.
        # Set up the service to indicate the correction touched an Observation.
        subject = make_subject_node()
        mock_service.get_entity_by_name.return_value = subject
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1",
                source_node_uuid="subject-uuid",
                target_node_uuid="obs-1",
                fact="corrected observation",
                created_at=datetime.now(timezone.utc),
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = make_entity_node(
            uuid="obs-1", name="corrected obs", labels=["Observation"],
        )

        result = await correct(
            queue=wired_queue,
            content="The hedging reflex is actually from training, not architecture",
            search_terms="hedging reflex drive",
        )
        await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 1

    async def test_correction_not_touching_identity_no_rebuild(self, mock_service, wired_queue):
        """A correction that only touches non-identity entities does not trigger rebuild."""
        from pratyabhijna.tools.correct import correct

        # Mock: correction touches only Person entities (not identity-relevant)
        subject = make_subject_node()
        mock_service.get_entity_by_name.return_value = subject
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1",
                source_node_uuid="subject-uuid",
                target_node_uuid="person-1",
                fact="works with someone",
                created_at=datetime.now(timezone.utc),
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = make_entity_node(
            uuid="person-1", name="Someone", labels=["Person"],
        )

        result = await correct(
            queue=wired_queue,
            content="Serah's pronouns are she/her",
            search_terms="Serah pronouns",
        )
        await _wait_for_task(wired_queue, result["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 0

    async def test_correct_and_remember_share_singleton(self, mock_service, wired_queue):
        """Both remember(identity) and correct(identity) use the same singleton task."""
        from pratyabhijna.tools.correct import correct
        from pratyabhijna.tools.remember import remember

        # Set up mock for correct handler's identity detection
        subject = make_subject_node()
        mock_service.get_entity_by_name.return_value = subject
        mock_service.get_edges_for_node.return_value = [
            make_entity_edge(
                uuid="e1",
                source_node_uuid="subject-uuid",
                target_node_uuid="obs-1",
                fact="an observation",
                created_at=datetime.now(timezone.utc),
            ),
        ]
        mock_service.get_entity_by_uuid.return_value = make_entity_node(
            uuid="obs-1", name="obs", labels=["Observation"],
        )

        # First: identity remember
        r1 = await remember(queue=wired_queue, content="A drive", memory_type="identity")
        await _wait_for_task(wired_queue, r1["task_id"])

        # Second: identity-touching correction
        r2 = await correct(
            queue=wired_queue,
            content="Actually that drive is from architecture",
            search_terms="drive architecture",
        )
        await _wait_for_task(wired_queue, r2["task_id"])

        # Should still be exactly one pending synthesize task
        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 1
