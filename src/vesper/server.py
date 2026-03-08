"""Vesper MCP server entry point.

Creates a FastMCP server with all seven tools registered.
Tools are stubs until their respective phases are implemented.
"""

from mcp.server.fastmcp import FastMCP


def create_server() -> FastMCP:
    """Create and configure the Vesper MCP server."""
    server = FastMCP("vesper")

    # --- Phase 1: status ---

    @server.tool()
    async def status() -> dict:
        """System orientation — DB health, queue depth, last write time."""
        from vesper.tools.status import status as _status

        return await _status()

    # --- Phase 3: write tools (stubs until Phase 3) ---

    @server.tool()
    async def remember(
        content: str,
        memory_type: str = "observation",
        source: str = "vesper",
    ) -> dict:
        """Queue an observation, fact, reasoning, or identity item for processing."""
        raise NotImplementedError("remember is not yet implemented (Phase 3)")

    @server.tool()
    async def correct(
        content: str,
        search_terms: str,
    ) -> dict:
        """Queue a correction with temporal supersession."""
        raise NotImplementedError("correct is not yet implemented (Phase 3)")

    # --- Phase 4: read tools (stubs until Phase 4) ---

    @server.tool()
    async def recall(
        query: str,
        memory_type: str | None = None,
        time_range: str | None = None,
    ) -> dict:
        """Search memory with semantic + keyword + graph traversal."""
        raise NotImplementedError("recall is not yet implemented (Phase 4)")

    @server.tool()
    async def history(
        entity_name: str,
    ) -> dict:
        """Temporal evolution of an entity or topic."""
        raise NotImplementedError("history is not yet implemented (Phase 4)")

    @server.tool()
    async def inspect(
        uuid: str,
    ) -> dict:
        """Detailed view of a memory node or edge with connections."""
        raise NotImplementedError("inspect is not yet implemented (Phase 4)")

    # --- Phase 5: context (stub until Phase 5) ---

    @server.tool()
    async def context() -> dict:
        """Return cached identity synthesis + delta for reconstruction."""
        raise NotImplementedError("context is not yet implemented (Phase 5)")

    return server
