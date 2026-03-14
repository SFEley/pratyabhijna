"""Graphiti client wrapper and lifecycle management.

Manages the Graphiti client connection, initialization,
and shutdown. Provides the service layer between MCP tools
and graphiti-core.
"""

# Stub — implementation in Phase 1


class VesperService:
    """Wraps graphiti-core client with Vesper-specific lifecycle."""

    def __init__(self, config):
        self.config = config
        self._graphiti = None

    async def start(self):
        """Initialize the Graphiti client and connect to Neo4j."""
        raise NotImplementedError

    async def stop(self):
        """Shut down the Graphiti client."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        """Whether the graph DB connection is active."""
        return self._graphiti is not None
