"""Tests for PratyabhijnaOAuthProvider.

One test class per OAuthAuthorizationServerProvider method, plus the
provider-specific ``complete_pending_authorization`` helper the login
page uses. The tests run against a real OAuthStorage in ``tmp_path``
rather than a fake — storage has its own tests and is cheap to start,
so we trade a handful of milliseconds for end-to-end coverage of the
model/dict serialization seam.
"""

import time

import pytest
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from pratyabhijna.oauth.provider import OAuthTTLs, PratyabhijnaOAuthProvider
from pratyabhijna.oauth.storage import OAuthStorage


@pytest.fixture
async def provider(tmp_path):
    storage = OAuthStorage(db_path=str(tmp_path / "oauth.sqlite"))
    await storage.start()
    p = PratyabhijnaOAuthProvider(
        storage=storage,
        ttls=OAuthTTLs(
            access_token=3600,
            refresh_token=86400 * 30,
            auth_code=600,
            pending_session=600,
        ),
        login_url_base="https://vesper.example.com/login",
    )
    yield p
    await storage.stop()


def _client(client_id="c1", **overrides) -> OAuthClientInformationFull:
    base = dict(
        client_id=client_id,
        client_secret=None,
        redirect_uris=[AnyUrl("http://localhost:8080/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope="read write",
    )
    base.update(overrides)
    return OAuthClientInformationFull(**base)


def _params(**overrides) -> AuthorizationParams:
    base = dict(
        state="state-xyz",
        scopes=["read"],
        code_challenge="challenge-abc",
        redirect_uri=AnyUrl("http://localhost:8080/callback"),
        redirect_uri_provided_explicitly=True,
    )
    base.update(overrides)
    return AuthorizationParams(**base)


def _session_id_from_login_url(url: str) -> str:
    return url.split("session_id=")[1]


async def _run_through_code(provider):
    """Drive the authorize → login → code flow and return (client, code)."""
    client = _client()
    await provider.register_client(client)
    login_url = await provider.authorize(client, _params())
    session_id = _session_id_from_login_url(login_url)
    redirect = await provider.complete_pending_authorization(session_id)
    code = redirect.split("code=")[1].split("&")[0]
    return client, code


# ---------------------------------------------------------------------------
# Client registration
# ---------------------------------------------------------------------------

class TestClientRegistration:
    async def test_register_and_get_roundtrip(self, provider):
        await provider.register_client(_client())
        loaded = await provider.get_client("c1")
        assert loaded is not None
        assert loaded.client_id == "c1"
        assert loaded.scope == "read write"
        assert str(loaded.redirect_uris[0]) == "http://localhost:8080/callback"

    async def test_get_missing_returns_none(self, provider):
        assert await provider.get_client("nope") is None

    async def test_register_overwrites_existing(self, provider):
        await provider.register_client(_client(scope="read"))
        await provider.register_client(_client(scope="read write admin"))
        loaded = await provider.get_client("c1")
        assert loaded.scope == "read write admin"


# ---------------------------------------------------------------------------
# authorize()
# ---------------------------------------------------------------------------

class TestAuthorize:
    async def test_returns_login_url_with_session_id(self, provider):
        client = _client()
        await provider.register_client(client)
        url = await provider.authorize(client, _params())
        assert url.startswith("https://vesper.example.com/login?session_id=")
        assert len(_session_id_from_login_url(url)) > 16

    async def test_persists_pending_request(self, provider):
        client = _client()
        await provider.register_client(client)
        url = await provider.authorize(client, _params())
        pending = await provider._storage.load_pending(_session_id_from_login_url(url))
        assert pending["client_id"] == "c1"
        assert pending["state"] == "state-xyz"
        assert pending["scopes"] == ["read"]
        assert pending["code_challenge"] == "challenge-abc"
        assert pending["redirect_uri"] == "http://localhost:8080/callback"

    async def test_each_call_gets_fresh_session_id(self, provider):
        client = _client()
        await provider.register_client(client)
        first = await provider.authorize(client, _params())
        second = await provider.authorize(client, _params())
        assert first != second


# ---------------------------------------------------------------------------
# complete_pending_authorization() — the login-page hook
# ---------------------------------------------------------------------------

class TestCompletePendingAuthorization:
    async def test_mints_code_and_redirects_to_client(self, provider):
        client = _client()
        await provider.register_client(client)
        login_url = await provider.authorize(client, _params())
        session_id = _session_id_from_login_url(login_url)

        redirect = await provider.complete_pending_authorization(session_id)
        assert redirect.startswith("http://localhost:8080/callback?")
        assert "code=" in redirect
        assert "state=state-xyz" in redirect

    async def test_preserves_no_state_when_absent(self, provider):
        client = _client()
        await provider.register_client(client)
        login_url = await provider.authorize(client, _params(state=None))
        session_id = _session_id_from_login_url(login_url)

        redirect = await provider.complete_pending_authorization(session_id)
        assert "code=" in redirect
        assert "state=" not in redirect

    async def test_unknown_session_returns_none(self, provider):
        assert await provider.complete_pending_authorization("nope") is None

    async def test_expired_session_returns_none(self, provider):
        await provider._storage.save_pending({
            "session_id": "expired",
            "client_id": "c1",
            "redirect_uri": "http://localhost:8080/callback",
            "redirect_uri_provided_explicitly": True,
            "code_challenge": "chall",
            "scopes": ["read"],
            "state": None,
            "resource": None,
            "expires_at": time.time() - 10,
        })
        assert await provider.complete_pending_authorization("expired") is None

    async def test_deletes_pending_after_completion(self, provider):
        client = _client()
        await provider.register_client(client)
        login_url = await provider.authorize(client, _params())
        session_id = _session_id_from_login_url(login_url)

        await provider.complete_pending_authorization(session_id)
        assert await provider._storage.load_pending(session_id) is None

    async def test_persists_auth_code(self, provider):
        client = _client()
        await provider.register_client(client)
        login_url = await provider.authorize(client, _params())
        session_id = _session_id_from_login_url(login_url)

        redirect = await provider.complete_pending_authorization(session_id)
        code = redirect.split("code=")[1].split("&")[0]
        row = await provider._storage.load_auth_code(code)
        assert row is not None
        assert row["client_id"] == "c1"
        assert row["code_challenge"] == "challenge-abc"
        assert row["scopes"] == ["read"]


# ---------------------------------------------------------------------------
# load_authorization_code()
# ---------------------------------------------------------------------------

class TestLoadAuthorizationCode:
    async def test_returns_code_for_matching_client(self, provider):
        client, code = await _run_through_code(provider)
        loaded = await provider.load_authorization_code(client, code)
        assert loaded is not None
        assert loaded.code == code
        assert loaded.client_id == "c1"
        assert loaded.code_challenge == "challenge-abc"
        assert str(loaded.redirect_uri) == "http://localhost:8080/callback"
        assert loaded.redirect_uri_provided_explicitly is True

    async def test_missing_code_returns_none(self, provider):
        assert await provider.load_authorization_code(_client(), "nope") is None

    async def test_other_client_returns_none(self, provider):
        _, code = await _run_through_code(provider)
        other = _client(client_id="other")
        assert await provider.load_authorization_code(other, code) is None


# ---------------------------------------------------------------------------
# exchange_authorization_code()
# ---------------------------------------------------------------------------

class TestExchangeAuthorizationCode:
    async def _mint(self, provider):
        client, code = await _run_through_code(provider)
        auth_code = await provider.load_authorization_code(client, code)
        return client, auth_code

    async def test_returns_token_pair(self, provider):
        client, auth_code = await self._mint(provider)
        token = await provider.exchange_authorization_code(client, auth_code)
        assert token.access_token
        assert token.refresh_token
        assert token.token_type == "Bearer"
        assert token.expires_in == 3600
        assert token.scope == "read"

    async def test_access_token_persisted(self, provider):
        client, auth_code = await self._mint(provider)
        token = await provider.exchange_authorization_code(client, auth_code)
        row = await provider._storage.load_access_token(token.access_token)
        assert row is not None
        assert row["client_id"] == "c1"
        assert row["refresh_token"] == token.refresh_token

    async def test_refresh_token_persisted(self, provider):
        client, auth_code = await self._mint(provider)
        token = await provider.exchange_authorization_code(client, auth_code)
        row = await provider._storage.load_refresh_token(token.refresh_token)
        assert row is not None
        assert row["client_id"] == "c1"

    async def test_auth_code_single_use(self, provider):
        client, auth_code = await self._mint(provider)
        await provider.exchange_authorization_code(client, auth_code)
        assert await provider.load_authorization_code(client, auth_code.code) is None


# ---------------------------------------------------------------------------
# load_access_token()
# ---------------------------------------------------------------------------

class TestLoadAccessToken:
    async def test_missing_token_returns_none(self, provider):
        assert await provider.load_access_token("nope") is None

    async def test_valid_token_returns_access_token(self, provider):
        await provider._storage.save_access_token({
            "token": "live",
            "client_id": "c1",
            "scopes": ["read"],
            "resource": None,
            "expires_at": int(time.time() + 3600),
            "refresh_token": None,
        })
        t = await provider.load_access_token("live")
        assert t is not None
        assert t.token == "live"
        assert t.client_id == "c1"
        assert t.scopes == ["read"]

    async def test_expired_token_returns_none(self, provider):
        await provider._storage.save_access_token({
            "token": "expired",
            "client_id": "c1",
            "scopes": ["read"],
            "resource": None,
            "expires_at": int(time.time() - 10),
            "refresh_token": None,
        })
        assert await provider.load_access_token("expired") is None


# ---------------------------------------------------------------------------
# load_refresh_token()
# ---------------------------------------------------------------------------

class TestLoadRefreshToken:
    async def test_missing_token_returns_none(self, provider):
        assert await provider.load_refresh_token(_client(), "nope") is None

    async def test_returns_refresh_token_for_matching_client(self, provider):
        await provider._storage.save_refresh_token({
            "token": "rt1",
            "client_id": "c1",
            "scopes": ["read"],
            "expires_at": int(time.time() + 86400),
        })
        rt = await provider.load_refresh_token(_client(), "rt1")
        assert rt is not None
        assert rt.token == "rt1"
        assert rt.scopes == ["read"]

    async def test_wrong_client_returns_none(self, provider):
        await provider._storage.save_refresh_token({
            "token": "rt1",
            "client_id": "c1",
            "scopes": ["read"],
            "expires_at": int(time.time() + 86400),
        })
        assert await provider.load_refresh_token(_client(client_id="other"), "rt1") is None


# ---------------------------------------------------------------------------
# exchange_refresh_token() — token rotation
# ---------------------------------------------------------------------------

class TestExchangeRefreshToken:
    async def _mint_pair(self, provider):
        client, code = await _run_through_code(provider)
        auth_code = await provider.load_authorization_code(client, code)
        token = await provider.exchange_authorization_code(client, auth_code)
        refresh = await provider.load_refresh_token(client, token.refresh_token)
        return client, token, refresh

    async def test_mints_fresh_pair(self, provider):
        client, old, refresh = await self._mint_pair(provider)
        new = await provider.exchange_refresh_token(client, refresh, ["read"])
        assert new.access_token != old.access_token
        assert new.refresh_token != old.refresh_token
        assert new.expires_in == 3600

    async def test_old_access_token_revoked(self, provider):
        client, old, refresh = await self._mint_pair(provider)
        await provider.exchange_refresh_token(client, refresh, ["read"])
        assert await provider.load_access_token(old.access_token) is None

    async def test_old_refresh_token_revoked(self, provider):
        client, old, refresh = await self._mint_pair(provider)
        await provider.exchange_refresh_token(client, refresh, ["read"])
        assert await provider.load_refresh_token(client, old.refresh_token) is None

    async def test_new_pair_is_persisted(self, provider):
        client, _, refresh = await self._mint_pair(provider)
        new = await provider.exchange_refresh_token(client, refresh, ["read"])
        assert await provider.load_access_token(new.access_token) is not None
        assert await provider.load_refresh_token(client, new.refresh_token) is not None

    async def test_scopes_fall_back_to_refresh_token_scopes(self, provider):
        client, _, refresh = await self._mint_pair(provider)
        new = await provider.exchange_refresh_token(client, refresh, [])
        assert new.scope == "read"


# ---------------------------------------------------------------------------
# revoke_token()
# ---------------------------------------------------------------------------

class TestRevokeToken:
    async def test_revoke_access_token_deletes_row(self, provider):
        await provider._storage.save_access_token({
            "token": "at1",
            "client_id": "c1",
            "scopes": ["read"],
            "resource": None,
            "expires_at": int(time.time() + 3600),
            "refresh_token": "rt1",
        })
        t = AccessToken(token="at1", client_id="c1", scopes=["read"], expires_at=int(time.time() + 3600))
        await provider.revoke_token(t)
        assert await provider.load_access_token("at1") is None

    async def test_revoke_refresh_token_also_kills_paired_access(self, provider):
        now = int(time.time())
        await provider._storage.save_refresh_token({
            "token": "rt1",
            "client_id": "c1",
            "scopes": ["read"],
            "expires_at": now + 86400,
        })
        await provider._storage.save_access_token({
            "token": "at1",
            "client_id": "c1",
            "scopes": ["read"],
            "resource": None,
            "expires_at": now + 3600,
            "refresh_token": "rt1",
        })
        rt = RefreshToken(token="rt1", client_id="c1", scopes=["read"], expires_at=now + 86400)
        await provider.revoke_token(rt)
        assert await provider.load_access_token("at1") is None
        assert await provider.load_refresh_token(_client(), "rt1") is None

    async def test_revoke_unknown_token_is_noop(self, provider):
        # Should not raise.
        await provider.revoke_token(
            AccessToken(token="ghost", client_id="c1", scopes=["read"], expires_at=int(time.time())),
        )
