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

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette

    from pratyabhijna.oauth.storage import OAuthStorage


def build_http_app(server: "FastMCP", oauth_storage: "OAuthStorage") -> "Starlette":
    """Return the FastMCP Starlette app with OAuth storage lifecycle attached.

    Replaces ``app.router.lifespan_context`` with a wrapper that runs
    ``oauth_storage.start()`` on ASGI startup and ``oauth_storage.stop()``
    on shutdown, then defers to FastMCP's original Starlette lifespan
    (which runs the StreamableHTTPSessionManager). Uvicorn invokes the
    wrapped lifespan before it begins accepting connections, so OAuth
    routes never see an unstarted database.
    """
    app = server.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def wrapped_lifespan(inner_app):
        await oauth_storage.start()
        try:
            async with original_lifespan(inner_app):
                yield
        finally:
            await oauth_storage.stop()

    app.router.lifespan_context = wrapped_lifespan
    return app
