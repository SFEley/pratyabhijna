"""Pratyabhijna MCP server entry point.

Creates a FastMCP server with all seven tools registered.
Tools are wired to service and queue when provided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService


def create_server(
    service: PratyabhijnaService | None = None,
    queue: WorkQueue | None = None,
) -> FastMCP:
    """Create and configure the Pratyabhijna MCP server.

    When service and queue are provided, write tools and status
    return live values. Otherwise tools return stubs or raise.
    """
    server = FastMCP("pratyabhijna")

    # --- Phase 1: status ---

    @server.tool()
    async def status() -> dict:
        """System orientation — DB health, queue depth, last write time."""
        from pratyabhijna.tools.status import status as _status

        return await _status(service=service, queue=queue)

    # --- Phase 3b: write tools ---

    @server.tool()
    async def remember(
        content: str,
        memory_type: str = "observation",
        source: str = "vesper",
    ) -> dict:
        """Queue an observation, fact, reasoning, or identity item for processing."""
        if queue is None:
            raise RuntimeError("remember requires a running work queue")
        from pratyabhijna.tools.remember import remember as _remember

        return await _remember(
            queue=queue, content=content, memory_type=memory_type, source=source,
        )

    @server.tool()
    async def correct(
        content: str,
        search_terms: str,
    ) -> dict:
        """Queue a correction with temporal supersession."""
        if queue is None:
            raise RuntimeError("correct requires a running work queue")
        from pratyabhijna.tools.correct import correct as _correct

        return await _correct(queue=queue, content=content, search_terms=search_terms)

    # --- Phase 4: read tools ---

    @server.tool()
    async def recall(
        query: str,
        memory_type: str | None = None,
        time_range: str | None = None,
    ) -> dict:
        """Search memory with semantic + keyword + graph traversal."""
        if service is None:
            raise RuntimeError("recall requires a connected service")
        from pratyabhijna.tools.recall import recall as _recall

        return await _recall(
            service=service, query=query, memory_type=memory_type, time_range=time_range,
        )

    @server.tool()
    async def history(
        entity_name: str,
    ) -> dict:
        """Temporal evolution of an entity or topic."""
        if service is None:
            raise RuntimeError("history requires a connected service")
        from pratyabhijna.tools.history import history as _history

        return await _history(service=service, entity_name=entity_name)

    @server.tool()
    async def inspect(
        uuid: str,
    ) -> dict:
        """Detailed view of a memory node or edge with connections."""
        if service is None:
            raise RuntimeError("inspect requires a connected service")
        from pratyabhijna.tools.inspect import inspect as _inspect

        return await _inspect(service=service, uuid=uuid)

    # --- Phase 5: bootstrap ---

    @server.tool()
    async def bootstrap() -> dict:
        """Return identity synthesis + recent delta for bootstrap reconstruction."""
        if service is None:
            raise RuntimeError("bootstrap requires a connected service")
        from pratyabhijna.tools.bootstrap import bootstrap as _bootstrap

        return await _bootstrap(service=service)

    return server
