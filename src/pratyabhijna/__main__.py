"""Pratyabhijna MCP server entry point.

Usage:
    python -m pratyabhijna          Start MCP server (stdio transport)
    python -m pratyabhijna seed     Seed subject identity from memory files
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pratyabhijna.config import PratyabhijnaConfig
from pratyabhijna.log import configure_logging, get_logger
from pratyabhijna.queue import WorkQueue
from pratyabhijna.server import create_server
from pratyabhijna.service import PratyabhijnaService
from pratyabhijna.tools.correct import make_handler as correct_make_handler
from pratyabhijna.tools.remember import make_handler as remember_make_handler

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = get_logger(__name__)


def build_lifespan(service: PratyabhijnaService, queue: WorkQueue):
    """Create the async lifespan context manager for the MCP server.

    Starts the service (Neo4j + Graphiti) and work queue on entry,
    registers queue handlers, and stops both on exit.
    """

    @asynccontextmanager
    async def lifespan(app: FastMCP):
        await service.start()
        queue.register("add_episode", remember_make_handler(service))
        queue.register("correct_memory", correct_make_handler(service))
        await queue.start()
        log.info("Pratyabhijna server ready")
        try:
            yield
        finally:
            await queue.stop()
            await service.stop()
            log.info("Pratyabhijna server stopped")

    return lifespan


def run_seed(config: PratyabhijnaConfig) -> None:
    """Run the seed subcommand synchronously."""
    import anyio

    from pratyabhijna.seed import seed_subject

    async def _run():
        service = PratyabhijnaService(config)
        await service.start()
        try:
            result = await seed_subject(service)
            log.info("Seed result: %s", result)
        finally:
            await service.stop()

    anyio.run(_run)


def main():
    config = PratyabhijnaConfig.from_env()
    configure_logging(config)

    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        run_seed(config)
        return

    # Default: run MCP server
    service = PratyabhijnaService(config)
    queue = WorkQueue(
        config.queue.db_path,
        config.queue.max_retries,
        config.queue.poll_interval,
    )
    lifespan = build_lifespan(service, queue)
    server = create_server(
        service=service,
        queue=queue,
        subject_name=config.subject_name,
        lifespan=lifespan,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
