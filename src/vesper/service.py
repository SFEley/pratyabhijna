"""Graphiti client wrapper and lifecycle management.

Manages the Graphiti client connection, initialization,
and shutdown. Provides the service layer between MCP tools
and graphiti-core.
"""

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver

from vesper.entity_types import VESPER_ENTITY_TYPES


class VesperService:
    """Wraps graphiti-core client with Vesper-specific lifecycle."""

    def __init__(self, config):
        self.config = config
        self._graphiti = None

    async def start(self):
        """Initialize the Graphiti client and connect to Neo4j."""
        neo4j = self.config.neo4j
        driver = Neo4jDriver(
            uri=neo4j.uri,
            user=neo4j.user,
            password=neo4j.password,
            database=neo4j.database,
        )
        self._graphiti = Graphiti(graph_driver=driver)

    async def stop(self):
        """Shut down the Graphiti client."""
        self._graphiti = None

    @property
    def is_connected(self) -> bool:
        """Whether the graph DB connection is active."""
        return self._graphiti is not None

    @property
    def entity_types(self) -> dict:
        """Entity type registry for use with add_episode()."""
        return VESPER_ENTITY_TYPES
