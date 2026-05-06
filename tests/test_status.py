"""Tests for the `status` MCP tool.

The status tool returns a nested system-orientation dict with three blocks:
``queue``, ``graph``, and ``synthesis``. Used by both the MCP server and
the `python -m pratyabhijna status` CLI subcommand — both paths call
``status(service, queue_db_path)`` so they surface the same information.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Wired mode — live service + queue db_path
# ---------------------------------------------------------------------------

def _make_service(
    connected=True,
    subject_name="Vesper",
    nodes_total=0,
    nodes_by_label=None,
    edges_total=0,
    edges_by_type=None,
    supersessions=0,
    subject_node=None,
    delta=None,
):
    """Build a mock service for status tests.

    Mocks the graph-count methods (count_nodes_total, count_nodes_by_label,
    count_edges_total, count_edges_by_type, count_supersessions) and a
    config object with subject_name. Synthesis data is driven by the
    `subject_node` and `delta` parameters, mocked at the synthesis helpers.
    """
    service = MagicMock()
    service.is_connected = connected
    service.config = MagicMock()
    service.config.subject_name = subject_name
    service.count_nodes_total = AsyncMock(return_value=nodes_total)
    service.count_nodes_by_label = AsyncMock(return_value=nodes_by_label or {})
    service.count_edges_total = AsyncMock(return_value=edges_total)
    service.count_edges_by_type = AsyncMock(return_value=edges_by_type or {})
    service.count_supersessions = AsyncMock(return_value=supersessions)
    service.count_episodes_since = AsyncMock(return_value=0)
    service.get_entity_by_name = AsyncMock(return_value=None)
    return service


class TestStatusTopLevel:
    """Top-level keys: version, db_connected, subject_name."""

    @pytest.mark.asyncio
    async def test_includes_version(self, tmp_path):
        from importlib.metadata import version as pkg_version

        from pratyabhijna.tools.status import status
        result = await status(service=_make_service(), queue_db_path=str(tmp_path / "q.db"))
        # Version comes from package metadata; assert against the same source
        # so the test never drifts from pyproject.toml.
        assert result["version"] == pkg_version("pratyabhijna")

    @pytest.mark.asyncio
    async def test_reflects_db_connected(self, tmp_path):
        from pratyabhijna.tools.status import status
        result = await status(
            service=_make_service(connected=True),
            queue_db_path=str(tmp_path / "q.db"),
        )
        assert result["db_connected"] is True

        result = await status(
            service=_make_service(connected=False),
            queue_db_path=str(tmp_path / "q.db"),
        )
        assert result["db_connected"] is False

    @pytest.mark.asyncio
    async def test_includes_subject_name(self, tmp_path):
        from pratyabhijna.tools.status import status
        result = await status(
            service=_make_service(subject_name="Vesper"),
            queue_db_path=str(tmp_path / "q.db"),
        )
        assert result["subject_name"] == "Vesper"


class TestStatusQueueBlock:
    """Queue block reflects live SQLite state via collect_queue_stats."""

    @pytest.mark.asyncio
    async def test_queue_block_is_nested_dict(self, tmp_path):
        from pratyabhijna.tools.status import status
        result = await status(
            service=_make_service(),
            queue_db_path=str(tmp_path / "q.db"),
        )
        assert "queue" in result
        assert isinstance(result["queue"], dict)

    @pytest.mark.asyncio
    async def test_queue_block_includes_core_fields(self, tmp_path):
        from pratyabhijna.tools.status import status
        result = await status(
            service=_make_service(),
            queue_db_path=str(tmp_path / "q.db"),
        )
        q = result["queue"]
        assert q["depth"] == 0
        assert q["last_write"] is None
        assert q["dead_letters"] == 0
        assert q["last_error"] is None
        assert q["by_task_type"] == {}

    @pytest.mark.asyncio
    async def test_queue_block_reflects_live_counts(self, tmp_path):
        """With real tasks in a SQLite file, the queue block matches."""
        from pratyabhijna.queue import WorkQueue
        from pratyabhijna.tools.status import status
        from helpers import wait_for

        async def _noop(payload: dict) -> None:
            pass

        db_path = str(tmp_path / "live_queue.sqlite")
        q = WorkQueue(db_path=db_path, poll_interval=0.05)
        q.register("add_episode", _noop)
        await q.start()
        await q.enqueue("add_episode", {})
        await q.enqueue("add_episode", {})

        async def all_done():
            return (await q.depth()) == 0

        await wait_for(all_done)
        await q.stop()

        result = await status(service=_make_service(), queue_db_path=db_path)
        assert result["queue"]["by_task_type"]["add_episode"]["completed"] == 2


class TestStatusGraphBlock:
    """Graph block reflects service count methods."""

    @pytest.mark.asyncio
    async def test_graph_block_is_nested_dict(self, tmp_path):
        from pratyabhijna.tools.status import status
        result = await status(
            service=_make_service(),
            queue_db_path=str(tmp_path / "q.db"),
        )
        assert "graph" in result
        assert isinstance(result["graph"], dict)

    @pytest.mark.asyncio
    async def test_graph_block_reflects_counts(self, tmp_path):
        from pratyabhijna.tools.status import status
        service = _make_service(
            nodes_total=42,
            nodes_by_label={"Entity": 40, "Episodic": 10, "Person": 2},
            edges_total=120,
            edges_by_type={"RELATES_TO": 100, "MENTIONS": 20},
            supersessions=3,
        )
        result = await status(service=service, queue_db_path=str(tmp_path / "q.db"))
        g = result["graph"]
        assert g["nodes_total"] == 42
        assert g["nodes_by_label"] == {"Entity": 40, "Episodic": 10, "Person": 2}
        assert g["edges_total"] == 120
        assert g["edges_by_type"] == {"RELATES_TO": 100, "MENTIONS": 20}
        assert g["supersessions"] == 3

    @pytest.mark.asyncio
    async def test_graph_block_handles_service_errors(self, tmp_path):
        """If a count method raises, status surfaces None/0 rather than failing."""
        from pratyabhijna.tools.status import status
        service = _make_service()
        service.count_nodes_total = AsyncMock(side_effect=RuntimeError("neo4j down"))
        result = await status(service=service, queue_db_path=str(tmp_path / "q.db"))
        assert "graph" in result
        # Status should never raise; degraded data is acceptable.
        assert result["graph"]["nodes_total"] is None


class TestStatusSynthesisBlock:
    """Synthesis block reflects subject node and identity delta."""

    @pytest.mark.asyncio
    async def test_synthesis_block_absent_subject(self, tmp_path, monkeypatch):
        """With no subject node, synthesis fields are None/0."""
        from pratyabhijna.tools.status import status

        async def fake_get_subject_node(service):
            return None

        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_node", fake_get_subject_node
        )

        result = await status(
            service=_make_service(), queue_db_path=str(tmp_path / "q.db")
        )
        s = result["synthesis"]
        assert s["last_run"] is None
        assert s["subject_delta_count"] is None
        assert s["new_episodes_count"] is None

    @pytest.mark.asyncio
    async def test_synthesis_block_with_subject(self, tmp_path, monkeypatch):
        """With a subject node, synthesis surfaces last_run and both counts.

        Legacy nodes (no ``synthesis_run_started_at``) fall back to
        ``context_rebuilt_at`` for ``last_run``.
        """
        from pratyabhijna.tools.status import status

        node = MagicMock()
        node.attributes = {"context_rebuilt_at": "2026-04-13T12:00:00+00:00"}

        async def fake_get_subject_node(service):
            return node

        async def fake_get_delta(service, subject_node):
            return ["d1", "d2", "d3"]

        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_node", fake_get_subject_node
        )
        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_delta", fake_get_delta
        )

        service = _make_service()
        service.count_episodes_since = AsyncMock(return_value=7)

        result = await status(
            service=service, queue_db_path=str(tmp_path / "q.db")
        )
        s = result["synthesis"]
        assert s["last_run"] == "2026-04-13T12:00:00+00:00"
        assert s["subject_delta_count"] == 3
        assert s["new_episodes_count"] == 7

    @pytest.mark.asyncio
    async def test_synthesis_block_prefers_run_start_over_rebuilt_at(
        self, tmp_path, monkeypatch
    ):
        """``last_run`` mirrors the delta's reference timestamp, so it
        prefers ``synthesis_run_started_at`` when present rather than the
        later ``context_rebuilt_at``."""
        from pratyabhijna.tools.status import status

        node = MagicMock()
        node.attributes = {
            "synthesis_run_started_at": "2026-04-13T11:00:00+00:00",
            "context_rebuilt_at": "2026-04-13T12:00:00+00:00",
        }

        async def fake_get_subject_node(service):
            return node

        async def fake_get_delta(service, subject_node):
            return []

        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_node", fake_get_subject_node
        )
        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_delta", fake_get_delta
        )

        result = await status(
            service=_make_service(), queue_db_path=str(tmp_path / "q.db")
        )
        assert result["synthesis"]["last_run"] == "2026-04-13T11:00:00+00:00"

    @pytest.mark.asyncio
    async def test_new_episodes_count_anchored_to_run_start(
        self, tmp_path, monkeypatch
    ):
        """``new_episodes_count`` is computed against
        ``synthesis_run_started_at`` (not ``context_rebuilt_at``), so
        episodes added during a still-in-progress run are visible to
        the next caller."""
        from datetime import datetime, timezone

        from pratyabhijna.tools.status import status

        node = MagicMock()
        node.attributes = {
            "synthesis_run_started_at": "2026-04-13T11:00:00+00:00",
            "context_rebuilt_at": "2026-04-13T12:00:00+00:00",
        }

        async def fake_get_subject_node(service):
            return node

        async def fake_get_delta(service, subject_node):
            return []

        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_node", fake_get_subject_node
        )
        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_delta", fake_get_delta
        )

        service = _make_service()
        service.count_episodes_since = AsyncMock(return_value=5)

        result = await status(
            service=service, queue_db_path=str(tmp_path / "q.db")
        )

        # Reference timestamp is the run-start, not the rebuild-at.
        service.count_episodes_since.assert_awaited_once()
        called_with = service.count_episodes_since.await_args.args[0]
        assert called_with == datetime(
            2026, 4, 13, 11, 0, 0, tzinfo=timezone.utc
        )
        assert result["synthesis"]["new_episodes_count"] == 5

    @pytest.mark.asyncio
    async def test_new_episodes_count_when_run_never_stamped(
        self, tmp_path, monkeypatch
    ):
        """A subject with no run-start and no rebuild timestamp passes
        ``since=None`` to ``count_episodes_since`` — every episode in
        the graph counts as new."""
        from pratyabhijna.tools.status import status

        node = MagicMock()
        node.attributes = {}

        async def fake_get_subject_node(service):
            return node

        async def fake_get_delta(service, subject_node):
            return []

        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_node", fake_get_subject_node
        )
        monkeypatch.setattr(
            "pratyabhijna.synthesis.get_subject_delta", fake_get_delta
        )

        service = _make_service()
        service.count_episodes_since = AsyncMock(return_value=42)

        result = await status(
            service=service, queue_db_path=str(tmp_path / "q.db")
        )

        service.count_episodes_since.assert_awaited_once_with(None)
        assert result["synthesis"]["new_episodes_count"] == 42
