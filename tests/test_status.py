"""Tests for the `status` MCP tool.

Verifies that `status` returns system health info including
graph DB connection state, queue depth, and last write timestamp.
"""

import pytest
from datetime import datetime, timezone


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
