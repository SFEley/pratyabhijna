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

    # Build auth if server URL and API key are configured.
    token_verifier = None
    auth = None
    if config.server.url and config.api_key:
        from mcp.server.auth.settings import AuthSettings

        from pratyabhijna.auth import StaticTokenVerifier

        token_verifier = StaticTokenVerifier(config.api_key)
        auth = AuthSettings(
            issuer_url=config.server.url,
            resource_server_url=config.server.url,
        )
        log.info("Bearer token auth enabled (resource server: %s)", config.server.url)
    elif config.server.url or config.api_key:
        log.warning(
            "Both PRATYABHIJNA_SERVER__URL and PRATYABHIJNA_API_KEY must be set to enable auth; "
            "running without authentication"
        )

    service = PratyabhijnaService(config)
    queue = WorkQueue(
        config.queue.db_path,
        config.queue.max_retries,
        config.queue.poll_interval,
        config.queue.backoff_base_seconds,
    )
    lifespan = build_lifespan(service, queue)
    # When running behind a reverse proxy (server.url set), disable FastMCP's
    # DNS rebinding protection — the proxy forwards Host: <external-domain>,
    # which the localhost allowlist would reject. Bearer token auth covers us.
    from mcp.server.fastmcp.server import TransportSecuritySettings

    transport_security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=False)
        if config.server.url
        else None  # let FastMCP auto-configure for localhost
    )

    server = create_server(
        service=service,
        queue=queue,
        subject_name=config.subject_name,
        lifespan=lifespan,
        token_verifier=token_verifier,
        auth=auth,
        host="127.0.0.1",
        port=config.server.port,
        transport_security=transport_security,
    )

    if config.server.url:
        log.info("Starting streamable-http transport on 127.0.0.1:%d", config.server.port)
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
