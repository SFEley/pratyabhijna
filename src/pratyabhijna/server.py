"""Pratyabhijna MCP server entry point.

Creates a FastMCP server with all seven tools registered.
Tools are wired to service and queue when provided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from mcp.server.auth.settings import AuthSettings

    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService


def create_server(
    service: PratyabhijnaService | None = None,
    queue: WorkQueue | None = None,
    subject_name: str | None = None,
    lifespan=None,
    auth_server_provider: Any | None = None,
    auth: AuthSettings | None = None,
    host: str = "127.0.0.1",
    port: int = 3000,
    transport_security=None,
    repo_path: str | None = None,
    resource_directories: list[str] | None = None,
) -> FastMCP:
    """Create and configure the Pratyabhijna MCP server.

    The MCP server identifies itself as ``Pratyabhijna`` — the service
    is the mirror, not the face. ``subject_name`` is passed through to
    tools (e.g. bootstrap) that need to know whose Person node to load,
    but it does not appear in the server's advertised name.

    When service and queue are provided, write tools and status
    return live values. Otherwise tools return stubs or raise.

    The optional lifespan async context manager handles service and
    queue startup/shutdown when running as a standalone server.
    """
    server = FastMCP(
        "Pratyabhijna",
        lifespan=lifespan,
        auth_server_provider=auth_server_provider,
        auth=auth,
        host=host,
        port=port,
        transport_security=transport_security,
    )

    # --- Phase 8: resources ---

    from pratyabhijna.resources import register_resources

    register_resources(server, repo_path, resource_directories)

    # --- Phase 1: status ---

    @server.tool()
    async def status() -> dict:
        """System orientation — queue, graph, and synthesis blocks."""
        from pratyabhijna.tools.status import status as _status

        return await _status(service=service, queue_db_path=queue.db_path)

    # --- Phase 3b: write tools ---

    @server.tool()
    async def remember(
        content: str,
        memory_type: str = "observation",
        source: str = "self",
        occurred_at: str | None = None,
        saga: str | None = None,
        saga_previous_episode_uuid: str | None = None,
    ) -> dict:
        """Queue an observation, fact, reasoning, or identity item for processing.

        occurred_at: optional ISO-8601 timestamp for when the fact was true in
        the world. Defaults to now. Use for retrospective memories whose
        occurrence predates the moment of recording.

        saga: optional saga name to group this episode into an ordered sequence
        (e.g. "solo-sessions"). Use saga_previous_episode_uuid to chain it
        after a specific prior episode in the same saga.
        """
        if queue is None:
            raise RuntimeError("remember requires a running work queue")
        from pratyabhijna.tools.remember import remember as _remember

        return await _remember(
            queue=queue,
            content=content,
            memory_type=memory_type,
            source=source,
            occurred_at=occurred_at,
            saga=saga,
            saga_previous_episode_uuid=saga_previous_episode_uuid,
        )

    @server.tool()
    async def correct(
        content: str,
        search_terms: str,
        occurred_at: str | None = None,
    ) -> dict:
        """Queue a correction with temporal supersession.

        occurred_at: optional ISO-8601 timestamp for when the corrected fact
        was true in the world. Defaults to now.
        """
        if queue is None:
            raise RuntimeError("correct requires a running work queue")
        from pratyabhijna.tools.correct import correct as _correct

        return await _correct(
            queue=queue,
            content=content,
            search_terms=search_terms,
            occurred_at=occurred_at,
        )

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
        """Return identity tiers, synthesis delta, and available tools for session start.

        Call first at the beginning of every session. The response includes the full
        list of tools available during the session — notably `remember` (write a new
        memory) and `correct` (fix a wrong memory), in addition to read tools.
        """
        if service is None:
            raise RuntimeError("bootstrap requires a connected service")
        from pratyabhijna.tools.bootstrap import bootstrap as _bootstrap

        return await _bootstrap(service=service, queue=queue)

    return server
