"""Tests for write-triggered synthesis rebuilds.

Synthesis scheduling moved to the bootstrap path (Phase 9). Write
handlers (remember, correct) no longer schedule synthesis directly,
with one exception: the correct handler still schedules synthesis when
the corrected content touches identity-typed entities. This is a
belt-and-suspenders trigger that fires in real time, whereas bootstrap
fires at session start.

The remember handler never schedules synthesis — it processes episodes
only. The bootstrap path is the primary synthesis trigger.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_entity_edge, make_entity_node, make_subject_node


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDENTITY_LABELS = {"Observation", "Drive", "Concept", "Question", "Thread"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_service():
    """Mock service with graphiti and config for trigger tests.

    Async methods the correct handler reaches during identity-neighbor
    detection (get_entity_by_name, get_edges_for_node, get_entity_by_uuid)
    are AsyncMock so they can be awaited. Tests override ``.return_value``
    on each to script the scenario.
    """
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
    service.config.add_episode.use_in_house = False
    service.get_entity_by_name = AsyncMock(return_value=None)
    service.get_edges_for_node = AsyncMock(return_value=[])
    service.get_entity_by_uuid = AsyncMock(return_value=None)
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
    async def test_no_memory_type_triggers_synthesis(self, mock_service, wired_queue):
        """remember never schedules synthesis regardless of memory_type.

        Synthesis is triggered by bootstrap, not by write handlers.
        """
        from pratyabhijna.tools.remember import remember

        for mem_type in ("observation", "identity", "reasoning", "fact", "position"):
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

    async def test_two_corrections_share_singleton(self, mock_service, wired_queue):
        """Two identity-touching corrections collapse into one pending synthesize task."""
        from pratyabhijna.tools.correct import correct

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

        r1 = await correct(
            queue=wired_queue,
            content="The hedging reflex is from training",
            search_terms="hedging reflex",
        )
        await _wait_for_task(wired_queue, r1["task_id"])

        r2 = await correct(
            queue=wired_queue,
            content="The execution eagerness is architectural",
            search_terms="execution eagerness",
        )
        await _wait_for_task(wired_queue, r2["task_id"])

        tasks = await _get_pending_synthesize_tasks(wired_queue)
        assert len(tasks) == 1
