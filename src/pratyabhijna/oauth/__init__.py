"""OAuth 2.1 authorization server for Pratyabhijna MCP access.

Implements the ``OAuthAuthorizationServerProvider`` protocol from the
MCP SDK, with SQLite-backed persistence for dynamic clients, pending
authorizations, authorization codes, access tokens, and refresh tokens.

The package is structured as three layers:

* ``storage`` — dict-based persistence. Knows rows, not OAuth models.
* ``provider`` — the protocol implementation. Owns serialization
  between SDK models and storage dicts; enforces PKCE and expiry.
* ``login`` — minimal HTML login page. Validates the shared secret
  against ``config.api_key`` and completes pending authorizations.

This module also exposes ``build_http_app``, which wraps a FastMCP
instance's Starlette app with an ASGI lifespan that starts and stops
the OAuth storage. This is necessary because FastMCP's
``streamable_http_app()`` hardcodes its Starlette lifespan to the
session manager; OAuth routes are hit before any MCP session exists,
so storage must be brought up in a separate lifespan layer.
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pratyabhijna.log import get_logger

log = get_logger(__name__)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette

    from pratyabhijna.oauth.storage import OAuthStorage
    from pratyabhijna.queue import WorkQueue
    from pratyabhijna.service import PratyabhijnaService


def build_http_app(
    server: "FastMCP",
    service: "PratyabhijnaService",
    queue: "WorkQueue",
    oauth_storage: "OAuthStorage | None" = None,
) -> "Starlette":
    """Return the FastMCP Starlette app with service/queue lifecycle at ASGI startup.

    Replaces ``app.router.lifespan_context`` with a wrapper that starts the
    service and queue (including crash recovery) before uvicorn begins
    accepting connections. OAuth storage is also started here when provided.

    FastMCP's built-in lifespan only fires when an MCP session is established,
    so service/queue startup cannot live there — the queue worker would not run
    until the first client connects, and crashed tasks would not be recovered.
    """
    app = server.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def wrapped_lifespan(inner_app):
        await service.start()
        try:
            await queue.start()
            if oauth_storage is not None:
                try:
                    await oauth_storage.start()
                except BaseException:
                    await queue.stop()
                    raise
        except BaseException:
            await service.stop()
            raise
        try:
            async with original_lifespan(inner_app):
                yield
        finally:
            log.info("Pratyabhijna shutting down")
            try:
                if oauth_storage is not None:
                    await oauth_storage.stop()
            except Exception:
                log.exception("Error stopping OAuth storage")
            try:
                await queue.stop()
            except Exception:
                log.exception("Error stopping queue")
            await service.stop()

    app.router.lifespan_context = wrapped_lifespan
    return app
