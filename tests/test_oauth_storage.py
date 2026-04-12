"""Tests for OAuth SQLite storage.

Defines the OAuthStorage contract: one test class per table covering
save/load/delete, plus a class for paired revocation (deleting access
tokens by their associated refresh token) and a class for expiry purge.

Storage stores plain dicts — model-level serialization lives in the
provider layer, not here — so tests exercise dict round-trips.
"""

import time

import pytest

from pratyabhijna.oauth.storage import OAuthStorage


@pytest.fixture
async def storage(tmp_path):
    db_path = str(tmp_path / "oauth_test.sqlite")
    s = OAuthStorage(db_path=db_path)
    await s.start()
    yield s
    await s.stop()


def _pending(session_id="sess1", **overrides):
    base = {
        "session_id": session_id,
        "client_id": "c1",
        "redirect_uri": "http://localhost:8080/callback",
        "redirect_uri_provided_explicitly": True,
        "code_challenge": "challenge-xyz",
        "scopes": ["read", "write"],
        "state": "xyz-state",
        "resource": None,
        "expires_at": time.time() + 600,
    }
    base.update(overrides)
    return base


def _auth_code(code="code1", **overrides):
    base = {
        "code": code,
        "client_id": "c1",
        "redirect_uri": "http://localhost:8080/callback",
        "redirect_uri_provided_explicitly": True,
        "code_challenge": "challenge-xyz",
        "scopes": ["read"],
        "resource": None,
        "expires_at": time.time() + 300,
    }
    base.update(overrides)
    return base


def _access_token(token="at1", **overrides):
    base = {
        "token": token,
        "client_id": "c1",
        "scopes": ["read"],
        "resource": None,
        "expires_at": int(time.time() + 3600),
        "refresh_token": "rt1",
    }
    base.update(overrides)
    return base


def _refresh_token(token="rt1", **overrides):
    base = {
        "token": token,
        "client_id": "c1",
        "scopes": ["read"],
        "expires_at": int(time.time() + 86400 * 30),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

class TestInit:
    async def test_start_creates_db(self, tmp_path):
        db_path = tmp_path / "oauth.sqlite"
        assert not db_path.exists()
        s = OAuthStorage(db_path=str(db_path))
        await s.start()
        assert db_path.exists()
        await s.stop()

    async def test_start_creates_parent_dirs(self, tmp_path):
        db_path = tmp_path / "nested" / "dir" / "oauth.sqlite"
        s = OAuthStorage(db_path=str(db_path))
        await s.start()
        assert db_path.exists()
        await s.stop()


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

class TestClients:
    async def test_save_and_load(self, storage):
        await storage.save_client("client-abc", '{"client_id": "client-abc"}')
        assert await storage.load_client("client-abc") == '{"client_id": "client-abc"}'

    async def test_load_missing_returns_none(self, storage):
        assert await storage.load_client("nope") is None

    async def test_save_overwrites_existing(self, storage):
        await storage.save_client("c1", '{"v": 1}')
        await storage.save_client("c1", '{"v": 2}')
        assert await storage.load_client("c1") == '{"v": 2}'


# ---------------------------------------------------------------------------
# Pending authorizations
# ---------------------------------------------------------------------------

class TestPendingAuthorizations:
    async def test_save_and_load(self, storage):
        await storage.save_pending(_pending())
        loaded = await storage.load_pending("sess1")
        assert loaded["client_id"] == "c1"
        assert loaded["scopes"] == ["read", "write"]
        assert loaded["redirect_uri_provided_explicitly"] is True
        assert loaded["state"] == "xyz-state"
        assert loaded["code_challenge"] == "challenge-xyz"

    async def test_load_missing_returns_none(self, storage):
        assert await storage.load_pending("nope") is None

    async def test_delete(self, storage):
        await storage.save_pending(_pending())
        await storage.delete_pending("sess1")
        assert await storage.load_pending("sess1") is None

    async def test_state_is_optional(self, storage):
        await storage.save_pending(_pending(session_id="s2", state=None))
        loaded = await storage.load_pending("s2")
        assert loaded["state"] is None


# ---------------------------------------------------------------------------
# Authorization codes
# ---------------------------------------------------------------------------

class TestAuthCodes:
    async def test_save_and_load(self, storage):
        await storage.save_auth_code(_auth_code())
        loaded = await storage.load_auth_code("code1")
        assert loaded["client_id"] == "c1"
        assert loaded["scopes"] == ["read"]
        assert loaded["redirect_uri_provided_explicitly"] is True
        assert loaded["code_challenge"] == "challenge-xyz"

    async def test_load_missing_returns_none(self, storage):
        assert await storage.load_auth_code("nope") is None

    async def test_delete(self, storage):
        await storage.save_auth_code(_auth_code())
        await storage.delete_auth_code("code1")
        assert await storage.load_auth_code("code1") is None


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------

class TestAccessTokens:
    async def test_save_and_load(self, storage):
        await storage.save_access_token(_access_token())
        loaded = await storage.load_access_token("at1")
        assert loaded["client_id"] == "c1"
        assert loaded["scopes"] == ["read"]
        assert loaded["refresh_token"] == "rt1"

    async def test_load_missing_returns_none(self, storage):
        assert await storage.load_access_token("nope") is None

    async def test_delete(self, storage):
        await storage.save_access_token(_access_token())
        await storage.delete_access_token("at1")
        assert await storage.load_access_token("at1") is None

    async def test_null_expires_at_and_refresh(self, storage):
        await storage.save_access_token(
            _access_token(token="at2", expires_at=None, refresh_token=None),
        )
        loaded = await storage.load_access_token("at2")
        assert loaded["expires_at"] is None
        assert loaded["refresh_token"] is None


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------

class TestRefreshTokens:
    async def test_save_and_load(self, storage):
        await storage.save_refresh_token(_refresh_token())
        loaded = await storage.load_refresh_token("rt1")
        assert loaded["client_id"] == "c1"
        assert loaded["scopes"] == ["read"]

    async def test_load_missing_returns_none(self, storage):
        assert await storage.load_refresh_token("nope") is None

    async def test_delete(self, storage):
        await storage.save_refresh_token(_refresh_token())
        await storage.delete_refresh_token("rt1")
        assert await storage.load_refresh_token("rt1") is None


# ---------------------------------------------------------------------------
# Paired revocation
# ---------------------------------------------------------------------------

class TestPairedRevocation:
    async def test_delete_access_tokens_by_refresh(self, storage):
        # Two access tokens paired with rt1, one with rt2.
        for token, rt in [("at1", "rt1"), ("at2", "rt1"), ("at3", "rt2")]:
            await storage.save_access_token(_access_token(token=token, refresh_token=rt))
        await storage.delete_access_tokens_by_refresh("rt1")
        assert await storage.load_access_token("at1") is None
        assert await storage.load_access_token("at2") is None
        assert await storage.load_access_token("at3") is not None


# ---------------------------------------------------------------------------
# Expiry purge
# ---------------------------------------------------------------------------

class TestPurgeExpired:
    async def test_purge_removes_expired_pending(self, storage):
        await storage.save_pending(_pending(session_id="expired", expires_at=time.time() - 10))
        await storage.save_pending(_pending(session_id="live"))
        await storage.purge_expired()
        assert await storage.load_pending("expired") is None
        assert await storage.load_pending("live") is not None

    async def test_purge_removes_expired_codes(self, storage):
        await storage.save_auth_code(_auth_code(code="old", expires_at=time.time() - 10))
        await storage.save_auth_code(_auth_code(code="new"))
        await storage.purge_expired()
        assert await storage.load_auth_code("old") is None
        assert await storage.load_auth_code("new") is not None

    async def test_purge_removes_expired_access_tokens(self, storage):
        await storage.save_access_token(
            _access_token(token="expired-at", expires_at=int(time.time() - 10)),
        )
        await storage.save_access_token(_access_token(token="live-at"))
        await storage.save_access_token(
            _access_token(token="no-expiry-at", expires_at=None),
        )
        await storage.purge_expired()
        assert await storage.load_access_token("expired-at") is None
        assert await storage.load_access_token("live-at") is not None
        # Tokens with NULL expiry are not purged — a missing expiry means
        # "never expires," which belongs to the revoke path, not the purge.
        assert await storage.load_access_token("no-expiry-at") is not None

    async def test_purge_removes_expired_refresh_tokens(self, storage):
        await storage.save_refresh_token(
            _refresh_token(token="expired-rt", expires_at=int(time.time() - 10)),
        )
        await storage.save_refresh_token(_refresh_token(token="live-rt"))
        await storage.purge_expired()
        assert await storage.load_refresh_token("expired-rt") is None
        assert await storage.load_refresh_token("live-rt") is not None
