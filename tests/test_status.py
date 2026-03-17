"""Tests for the `status` MCP tool.

Verifies that `status` returns system health info including
graph DB connection state, queue depth, and last write timestamp.

Phase 1 tests use the stub (no service/queue). Phase 3b tests
verify that status returns live values from wired service and queue.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Phase 1: stub tests (no service/queue wired)
# ---------------------------------------------------------------------------

class TestStatusTool:
    """The status tool returns system orientation info."""

    @pytest.mark.asyncio
    async def test_status_returns_dict(self):
        """status() returns a dictionary of system info."""
        from vesper.tools.status import status

        result = await status()
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_status_includes_db_connected(self):
        """status() reports whether the graph DB is connected."""
        from vesper.tools.status import status

        result = await status()
        assert "db_connected" in result
        assert isinstance(result["db_connected"], bool)

    @pytest.mark.asyncio
    async def test_status_includes_queue_depth(self):
        """status() reports the number of pending tasks in the work queue."""
        from vesper.tools.status import status

        result = await status()
        assert "queue_depth" in result
        assert isinstance(result["queue_depth"], int)
        assert result["queue_depth"] >= 0

    @pytest.mark.asyncio
    async def test_status_includes_last_write(self):
        """status() reports the timestamp of the last write operation.

        Returns None if no writes have occurred.
        """
        from vesper.tools.status import status

        result = await status()
        assert "last_write" in result
        # On a fresh system, last_write should be None
        # After writes, it should be an ISO timestamp string

    @pytest.mark.asyncio
    async def test_status_includes_version(self):
        """status() reports the server version."""
        from vesper.tools.status import status

        result = await status()
        assert "version" in result
        assert result["version"] == "0.1.0"


# ---------------------------------------------------------------------------
# Phase 3b: wired status (live service + queue values)
# ---------------------------------------------------------------------------

async def _noop_handler(payload: dict) -> None:
    """Handler that succeeds immediately."""


class TestStatusWired:
    """Status tool returns live values when service and queue are provided."""

    async def test_status_returns_live_db_state(self):
        """db_connected reflects service.is_connected."""
        from vesper.tools.status import status

        service = MagicMock()
        service.is_connected = True
        queue = MagicMock()
        queue.depth = AsyncMock(return_value=0)
        queue.last_write = AsyncMock(return_value=None)

        result = await status(service=service, queue=queue)
        assert result["db_connected"] is True

        service.is_connected = False
        result = await status(service=service, queue=queue)
        assert result["db_connected"] is False

    async def test_status_returns_live_queue_depth(self, tmp_path):
        """queue_depth reflects the actual queue depth."""
        from vesper.queue import WorkQueue
        from vesper.tools.status import status

        q = WorkQueue(db_path=str(tmp_path / "status_test.sqlite"), poll_interval=0.05)
        q.register("test", _noop_handler)
        await q.start()

        service = MagicMock()
        service.is_connected = True

        # Empty queue
        result = await status(service=service, queue=q)
        assert result["queue_depth"] == 0

        # Enqueue a task — stop worker first so it stays pending
        await q.stop()
        q2 = WorkQueue(db_path=str(tmp_path / "status_test.sqlite"), poll_interval=0.05)
        q2.register("test", _noop_handler)
        # Don't start the worker — just open the DB for queries
        await q2.start()
        await q2.enqueue("test", {"data": "value"})
        result = await status(service=service, queue=q2)
        assert result["queue_depth"] >= 1
        await q2.stop()

    async def test_status_returns_live_last_write(self):
        """last_write reflects queue.last_write()."""
        from vesper.tools.status import status

        service = MagicMock()
        service.is_connected = True
        queue = MagicMock()
        queue.depth = AsyncMock(return_value=0)
        queue.last_write = AsyncMock(return_value="2026-03-17T10:00:00+00:00")

        result = await status(service=service, queue=queue)
        assert result["last_write"] == "2026-03-17T10:00:00+00:00"
