"""The `status` MCP tool.

Returns system orientation info: DB connection state,
queue depth, last write timestamp, server version.
"""


async def status() -> dict:
    """Return system health info."""
    # Phase 1 implementation — returns static/stub values
    # until service and queue are wired up
    return {
        "version": "0.1.0",
        "db_connected": False,
        "queue_depth": 0,
        "last_write": None,
    }
