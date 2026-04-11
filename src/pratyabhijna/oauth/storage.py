"""SQLite-backed persistence for OAuth server state.

Five tables in a dedicated sqlite file:

* ``oauth_clients`` — dynamic clients (RFC 7591). Stored as opaque JSON
  blobs so storage needn't depend on SDK model types.
* ``oauth_pending_authorizations`` — ``/authorize`` requests awaiting
  user login. The ``session_id`` is the handle the login page uses to
  look up and complete the pending request.
* ``oauth_auth_codes`` — short-lived codes issued after login, ready
  for exchange at ``/token``.
* ``oauth_access_tokens`` / ``oauth_refresh_tokens`` — issued tokens.
  Each access token records its paired refresh token so revocation
  can find and delete both halves.

Values travel as plain dicts. Model-level serialization (Pydantic →
dict → row and back) lives in the provider layer, so this module can
be tested without the SDK installed and kept small.
"""

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from pratyabhijna.log import get_logger

_log = get_logger(__name__)


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id      TEXT PRIMARY KEY,
    client_info    TEXT NOT NULL,
    registered_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_pending_authorizations (
    session_id                        TEXT PRIMARY KEY,
    client_id                         TEXT NOT NULL,
    redirect_uri                      TEXT NOT NULL,
    redirect_uri_provided_explicitly  INTEGER NOT NULL,
    code_challenge                    TEXT NOT NULL,
    scopes                            TEXT NOT NULL,
    state                             TEXT,
    resource                          TEXT,
    expires_at                        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_pending_expires
    ON oauth_pending_authorizations(expires_at);

CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code                              TEXT PRIMARY KEY,
    client_id                         TEXT NOT NULL,
    redirect_uri                      TEXT NOT NULL,
    redirect_uri_provided_explicitly  INTEGER NOT NULL,
    code_challenge                    TEXT NOT NULL,
    scopes                            TEXT NOT NULL,
    resource                          TEXT,
    expires_at                        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_oauth_auth_codes_expires
    ON oauth_auth_codes(expires_at);

CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token          TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    scopes         TEXT NOT NULL,
    resource       TEXT,
    expires_at     INTEGER,
    refresh_token  TEXT
);
CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_client
    ON oauth_access_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_refresh
    ON oauth_access_tokens(refresh_token);
CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_expires
    ON oauth_access_tokens(expires_at);

CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token          TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL,
    scopes         TEXT NOT NULL,
    expires_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_client
    ON oauth_refresh_tokens(client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_expires
    ON oauth_refresh_tokens(expires_at);
"""


class OAuthStorage:
    """Async SQLite persistence for the OAuth authorization server.

    Designed to live in its own sqlite file alongside the work queue,
    with a shorter retention profile: most rows either expire or are
    deleted explicitly during the OAuth lifecycle.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        _log.debug("oauth storage opened at %s", self.db_path)

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- clients ------------------------------------------------------------

    async def save_client(self, client_id: str, client_info_json: str) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO oauth_clients "
            "(client_id, client_info, registered_at) VALUES (?, ?, ?)",
            (client_id, client_info_json, time.time()),
        )
        await self._db.commit()

    async def load_client(self, client_id: str) -> str | None:
        async with self._db.execute(
            "SELECT client_info FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row["client_info"] if row else None

    # --- pending authorizations --------------------------------------------

    async def save_pending(self, session: dict[str, Any]) -> None:
        await self._db.execute(
            """INSERT INTO oauth_pending_authorizations
               (session_id, client_id, redirect_uri, redirect_uri_provided_explicitly,
                code_challenge, scopes, state, resource, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session["session_id"],
                session["client_id"],
                session["redirect_uri"],
                int(bool(session["redirect_uri_provided_explicitly"])),
                session["code_challenge"],
                json.dumps(session["scopes"]),
                session.get("state"),
                session.get("resource"),
                session["expires_at"],
            ),
        )
        await self._db.commit()

    async def load_pending(self, session_id: str) -> dict[str, Any] | None:
        async with self._db.execute(
            "SELECT * FROM oauth_pending_authorizations WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _pending_row_to_dict(row)

    async def delete_pending(self, session_id: str) -> None:
        await self._db.execute(
            "DELETE FROM oauth_pending_authorizations WHERE session_id = ?",
            (session_id,),
        )
        await self._db.commit()

    # --- authorization codes -----------------------------------------------

    async def save_auth_code(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            """INSERT INTO oauth_auth_codes
               (code, client_id, redirect_uri, redirect_uri_provided_explicitly,
                code_challenge, scopes, resource, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["code"],
                record["client_id"],
                record["redirect_uri"],
                int(bool(record["redirect_uri_provided_explicitly"])),
                record["code_challenge"],
                json.dumps(record["scopes"]),
                record.get("resource"),
                record["expires_at"],
            ),
        )
        await self._db.commit()

    async def load_auth_code(self, code: str) -> dict[str, Any] | None:
        async with self._db.execute(
            "SELECT * FROM oauth_auth_codes WHERE code = ?",
            (code,),
        ) as cursor:
            row = await cursor.fetchone()
        return _auth_code_row_to_dict(row)

    async def delete_auth_code(self, code: str) -> None:
        await self._db.execute(
            "DELETE FROM oauth_auth_codes WHERE code = ?",
            (code,),
        )
        await self._db.commit()

    # --- access tokens -----------------------------------------------------

    async def save_access_token(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            """INSERT INTO oauth_access_tokens
               (token, client_id, scopes, resource, expires_at, refresh_token)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record["token"],
                record["client_id"],
                json.dumps(record["scopes"]),
                record.get("resource"),
                record.get("expires_at"),
                record.get("refresh_token"),
            ),
        )
        await self._db.commit()

    async def load_access_token(self, token: str) -> dict[str, Any] | None:
        async with self._db.execute(
            "SELECT * FROM oauth_access_tokens WHERE token = ?",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
        return _access_token_row_to_dict(row)

    async def delete_access_token(self, token: str) -> None:
        await self._db.execute(
            "DELETE FROM oauth_access_tokens WHERE token = ?",
            (token,),
        )
        await self._db.commit()

    async def delete_access_tokens_by_refresh(self, refresh_token: str) -> None:
        await self._db.execute(
            "DELETE FROM oauth_access_tokens WHERE refresh_token = ?",
            (refresh_token,),
        )
        await self._db.commit()

    # --- refresh tokens ----------------------------------------------------

    async def save_refresh_token(self, record: dict[str, Any]) -> None:
        await self._db.execute(
            """INSERT INTO oauth_refresh_tokens
               (token, client_id, scopes, expires_at)
               VALUES (?, ?, ?, ?)""",
            (
                record["token"],
                record["client_id"],
                json.dumps(record["scopes"]),
                record.get("expires_at"),
            ),
        )
        await self._db.commit()

    async def load_refresh_token(self, token: str) -> dict[str, Any] | None:
        async with self._db.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token = ?",
            (token,),
        ) as cursor:
            row = await cursor.fetchone()
        return _refresh_token_row_to_dict(row)

    async def delete_refresh_token(self, token: str) -> None:
        await self._db.execute(
            "DELETE FROM oauth_refresh_tokens WHERE token = ?",
            (token,),
        )
        await self._db.commit()

    # --- cleanup -----------------------------------------------------------

    async def purge_expired(self) -> None:
        """Delete rows whose expiry is in the past.

        Pending sessions and authorization codes always have an expiry.
        Access and refresh tokens may have ``expires_at = NULL`` (never
        expires), which this method intentionally leaves alone — removal
        of non-expiring tokens is the revoke path's job.
        """
        now = time.time()
        now_int = int(now)
        await self._db.execute(
            "DELETE FROM oauth_pending_authorizations WHERE expires_at <= ?",
            (now,),
        )
        await self._db.execute(
            "DELETE FROM oauth_auth_codes WHERE expires_at <= ?",
            (now,),
        )
        await self._db.execute(
            "DELETE FROM oauth_access_tokens "
            "WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_int,),
        )
        await self._db.execute(
            "DELETE FROM oauth_refresh_tokens "
            "WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now_int,),
        )
        await self._db.commit()


# ---------------------------------------------------------------------------
# Row → dict conversion
# ---------------------------------------------------------------------------

def _pending_row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "session_id": row["session_id"],
        "client_id": row["client_id"],
        "redirect_uri": row["redirect_uri"],
        "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
        "code_challenge": row["code_challenge"],
        "scopes": json.loads(row["scopes"]),
        "state": row["state"],
        "resource": row["resource"],
        "expires_at": row["expires_at"],
    }


def _auth_code_row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "code": row["code"],
        "client_id": row["client_id"],
        "redirect_uri": row["redirect_uri"],
        "redirect_uri_provided_explicitly": bool(row["redirect_uri_provided_explicitly"]),
        "code_challenge": row["code_challenge"],
        "scopes": json.loads(row["scopes"]),
        "resource": row["resource"],
        "expires_at": row["expires_at"],
    }


def _access_token_row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "token": row["token"],
        "client_id": row["client_id"],
        "scopes": json.loads(row["scopes"]),
        "resource": row["resource"],
        "expires_at": row["expires_at"],
        "refresh_token": row["refresh_token"],
    }


def _refresh_token_row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "token": row["token"],
        "client_id": row["client_id"],
        "scopes": json.loads(row["scopes"]),
        "expires_at": row["expires_at"],
    }
