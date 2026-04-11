"""End-to-end OAuth 2.1 flow against the real Starlette ASGI app.

These tests stand up a minimal FastMCP server with OAuth wired up but
no service/queue (the OAuth routes don't touch either). An
``httpx.AsyncClient`` drives the full dance: dynamic client
registration → authorize → login → token exchange → refresh → old
refresh token no longer valid. This exercises the provider, the
login routes, and FastMCP's auto-mounted OAuth routes as a single
unit, without requiring a real network bind.

DNS rebinding protection is disabled on the transport, matching the
production config (we sit behind Caddy, which rewrites the Host
header to the public domain).
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from pratyabhijna.oauth import build_http_app
from pratyabhijna.oauth.login import register_login_routes
from pratyabhijna.oauth.provider import OAuthTTLs, PratyabhijnaOAuthProvider
from pratyabhijna.oauth.storage import OAuthStorage


PASSWORD = "shared-secret-for-login"
ISSUER_URL = "https://vesper.example.com"
REDIRECT_URI = "http://localhost:8080/callback"


@pytest.fixture
async def oauth_app(tmp_path):
    """Return (asgi_app, provider) — storage is started, stopped on teardown.

    Storage is started directly here rather than through the Starlette
    lifespan because FastMCP's inner session-manager lifespan uses an
    anyio task group that can't cross a pytest-asyncio fixture yield
    boundary. The regression test for ``build_http_app``'s storage
    lifecycle lives in ``TestBuildHttpAppLifecycle`` below.
    """
    storage = OAuthStorage(db_path=str(tmp_path / "oauth.sqlite"))
    await storage.start()

    provider = PratyabhijnaOAuthProvider(
        storage=storage,
        ttls=OAuthTTLs(),  # defaults: 1h access, 30d refresh, 10m codes/sessions
        login_url_base=f"{ISSUER_URL}/login",
    )

    server = FastMCP(
        "Pratyabhijna-Test",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=ISSUER_URL,
            resource_server_url=ISSUER_URL,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
    register_login_routes(server, provider, PASSWORD)

    app = server.streamable_http_app()
    try:
        yield app, provider
    finally:
        await storage.stop()


def _pkce_pair() -> tuple[str, str]:
    """Generate an RFC 7636 code_verifier / S256 code_challenge pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def _register_client(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


async def _start_authorize(
    client: httpx.AsyncClient,
    client_id: str,
    challenge: str,
    state: str = "test-state",
) -> str:
    """Kick off /authorize and return the session_id from the redirect."""
    resp = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 307), resp.text
    location = resp.headers["location"]
    assert location.startswith(f"{ISSUER_URL}/login?session_id=")
    return location.split("session_id=")[1]


def _parse_callback(location: str) -> dict[str, str]:
    """Pull code/state out of a redirect URL."""
    parsed = urlparse(location)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


# ---------------------------------------------------------------------------
# The full happy path
# ---------------------------------------------------------------------------

class TestFullFlow:
    async def test_registration_through_refresh(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            # 1. Dynamic client registration
            client_id = await _register_client(client)

            # 2. Authorize → redirect into login page
            verifier, challenge = _pkce_pair()
            session_id = await _start_authorize(client, client_id, challenge)

            # 3. Submit correct password → redirect back with code
            login_resp = await client.post(
                "/login",
                data={"session_id": session_id, "password": PASSWORD},
                follow_redirects=False,
            )
            assert login_resp.status_code == 303, login_resp.text
            callback_params = _parse_callback(login_resp.headers["location"])
            assert "code" in callback_params
            assert callback_params["state"] == "test-state"
            code = callback_params["code"]

            # 4. Exchange code → access + refresh tokens
            token_resp = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                },
            )
            assert token_resp.status_code == 200, token_resp.text
            tokens = token_resp.json()
            assert tokens["token_type"].lower() == "bearer"
            assert tokens["expires_in"] == 3600
            assert tokens["access_token"]
            assert tokens["refresh_token"]
            access_token = tokens["access_token"]
            refresh_token = tokens["refresh_token"]

            # 5. Refresh → rotated pair
            refresh_resp = await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            assert refresh_resp.status_code == 200, refresh_resp.text
            new_tokens = refresh_resp.json()
            assert new_tokens["access_token"] != access_token
            assert new_tokens["refresh_token"] != refresh_token

            # 6. Old refresh token is now invalid (rotation)
            replay = await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
            )
            assert replay.status_code == 400


# ---------------------------------------------------------------------------
# Failure modes — login page and exchange
# ---------------------------------------------------------------------------

class TestLoginFailures:
    async def test_wrong_password_re_renders_form(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            client_id = await _register_client(client)
            _, challenge = _pkce_pair()
            session_id = await _start_authorize(client, client_id, challenge)

            bad = await client.post(
                "/login",
                data={"session_id": session_id, "password": "nope"},
                follow_redirects=False,
            )
            assert bad.status_code == 401
            assert "Incorrect secret." in bad.text
            assert 'name="session_id"' in bad.text

    async def test_unknown_session_renders_fatal_error(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            bad = await client.post(
                "/login",
                data={"session_id": "nope", "password": PASSWORD},
                follow_redirects=False,
            )
            assert bad.status_code == 400
            assert "expired" in bad.text.lower() or "no longer valid" in bad.text.lower()

    async def test_login_form_renders_on_get(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            resp = await client.get("/login", params={"session_id": "abc123"})
            assert resp.status_code == 200
            assert "<form" in resp.text
            assert 'value="abc123"' in resp.text


class TestTokenExchangeFailures:
    async def test_wrong_pkce_verifier_rejected(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            client_id = await _register_client(client)
            _, challenge = _pkce_pair()
            session_id = await _start_authorize(client, client_id, challenge)

            login_resp = await client.post(
                "/login",
                data={"session_id": session_id, "password": PASSWORD},
                follow_redirects=False,
            )
            code = _parse_callback(login_resp.headers["location"])["code"]

            # Use a random wrong verifier — the SDK should reject on PKCE mismatch.
            wrong_verifier = secrets.token_urlsafe(64)
            resp = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": wrong_verifier,
                },
            )
            assert resp.status_code == 400

    async def test_code_is_single_use(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            client_id = await _register_client(client)
            verifier, challenge = _pkce_pair()
            session_id = await _start_authorize(client, client_id, challenge)

            login_resp = await client.post(
                "/login",
                data={"session_id": session_id, "password": PASSWORD},
                follow_redirects=False,
            )
            code = _parse_callback(login_resp.headers["location"])["code"]

            first = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                },
            )
            assert first.status_code == 200

            second = await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": verifier,
                },
            )
            assert second.status_code == 400


# ---------------------------------------------------------------------------
# Protected resource + metadata
# ---------------------------------------------------------------------------

class TestProtectedResource:
    async def test_mcp_endpoint_requires_bearer_token(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            resp = await client.post("/mcp")
            assert resp.status_code == 401

    async def test_authorization_server_metadata_served(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            resp = await client.get("/.well-known/oauth-authorization-server")
            assert resp.status_code == 200
            meta = resp.json()
            assert meta["issuer"].rstrip("/") == ISSUER_URL
            assert "authorization_endpoint" in meta
            assert "token_endpoint" in meta
            assert "registration_endpoint" in meta

    async def test_protected_resource_metadata_served(self, oauth_app):
        app, _ = oauth_app
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ISSUER_URL,
        ) as client:
            resp = await client.get("/.well-known/oauth-protected-resource")
            assert resp.status_code == 200
            meta = resp.json()
            assert meta["resource"].rstrip("/") == ISSUER_URL
            assert len(meta["authorization_servers"]) >= 1


# ---------------------------------------------------------------------------
# build_http_app lifespan regression
# ---------------------------------------------------------------------------

class TestBuildHttpAppLifecycle:
    """Regression coverage for the production 500 bug.

    FastMCP's ``streamable_http_app()`` hardcodes its Starlette lifespan
    to ``self.session_manager.run()``, which only fires when an MCP
    session is established. OAuth routes (/register, /authorize, /token,
    /.well-known/*) are hit *before* any MCP session exists, so any
    startup hooked onto the FastMCP lifespan (like ``oauth_storage.start()``)
    never runs for them. The first DB call then raises ``AttributeError``,
    Starlette turns that into a plain-text 500, and the OAuth client
    chokes trying to JSON-parse the body.

    ``build_http_app`` fixes this by wrapping Starlette's outer
    ``router.lifespan_context`` so storage starts/stops at ASGI lifespan
    boundaries. These tests verify that wiring directly — without
    bringing up the session manager, which would collide with
    pytest-asyncio's fixture task boundaries.
    """

    async def test_wrapped_lifespan_starts_and_stops_storage(self, tmp_path):
        storage = OAuthStorage(db_path=str(tmp_path / "oauth.sqlite"))
        provider = PratyabhijnaOAuthProvider(
            storage=storage,
            ttls=OAuthTTLs(),
            login_url_base=f"{ISSUER_URL}/login",
        )
        server = FastMCP(
            "Pratyabhijna-Test",
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=ISSUER_URL,
                resource_server_url=ISSUER_URL,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
            ),
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            ),
        )
        register_login_routes(server, provider, PASSWORD)

        start_calls: list[str] = []
        stop_calls: list[str] = []
        real_start = storage.start
        real_stop = storage.stop

        async def tracking_start():
            start_calls.append("x")
            await real_start()

        async def tracking_stop():
            stop_calls.append("x")
            await real_stop()

        storage.start = tracking_start  # type: ignore[method-assign]
        storage.stop = tracking_stop  # type: ignore[method-assign]

        # Replace the inner FastMCP lifespan with a no-op so we can drive
        # the outer Starlette lifespan without spinning up the anyio
        # task group inside StreamableHTTPSessionManager.run().
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_lifespan(_inner):
            yield

        raw_app = server.streamable_http_app()
        raw_app.router.lifespan_context = noop_lifespan

        # Now wrap: build_http_app sees a Starlette app whose
        # lifespan_context is already noop_lifespan, and wraps it with
        # the oauth_storage start/stop.
        app = build_http_app(server, storage)

        assert start_calls == []
        async with app.router.lifespan_context(app):
            assert start_calls == ["x"]
            assert stop_calls == []
        assert stop_calls == ["x"]

    async def test_oauth_route_reachable_under_wrapped_lifespan(self, tmp_path):
        """After wrapped-lifespan startup, /.well-known is reachable without 500."""
        storage = OAuthStorage(db_path=str(tmp_path / "oauth.sqlite"))
        provider = PratyabhijnaOAuthProvider(
            storage=storage,
            ttls=OAuthTTLs(),
            login_url_base=f"{ISSUER_URL}/login",
        )
        server = FastMCP(
            "Pratyabhijna-Test",
            auth_server_provider=provider,
            auth=AuthSettings(
                issuer_url=ISSUER_URL,
                resource_server_url=ISSUER_URL,
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
            ),
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            ),
        )
        register_login_routes(server, provider, PASSWORD)

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def noop_lifespan(_inner):
            yield

        raw_app = server.streamable_http_app()
        raw_app.router.lifespan_context = noop_lifespan

        app = build_http_app(server, storage)

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=ISSUER_URL,
            ) as client:
                # Dynamic client registration touches storage. Under the
                # buggy wiring this would 500 with "Internal Server Error".
                resp = await client.post(
                    "/register",
                    json={
                        "redirect_uris": [REDIRECT_URI],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    },
                )
                assert resp.status_code == 201, resp.text
