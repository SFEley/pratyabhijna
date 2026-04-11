"""OAuth 2.1 authorization server provider for Pratyabhijna.

Implements the ``OAuthAuthorizationServerProvider`` protocol from the
MCP SDK on top of ``OAuthStorage``. The provider owns serialization
between SDK Pydantic models and storage-layer dicts, and token minting.

PKCE verification, auth-code expiry, redirect_uri consistency, and
scope validation at exchange time are all performed by the SDK's token
handler before it calls into this provider — see
``mcp/server/auth/handlers/token.py``. As a result, methods like
``exchange_authorization_code`` and ``exchange_refresh_token`` are
straight mint-and-persist paths with no security checks duplicated.

The /authorize flow is split across two calls:

* ``authorize()`` saves a pending record and returns a login URL with
  an opaque ``session_id`` query parameter.
* ``complete_pending_authorization()`` is called by the login page
  once the user has proven they know the shared secret. It mints an
  authorization code, deletes the pending record, and returns the
  client's redirect URL with ``code`` (and ``state`` if present).
"""

import secrets
import time
from dataclasses import dataclass

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from pratyabhijna.log import get_logger
from pratyabhijna.oauth.storage import OAuthStorage

_log = get_logger(__name__)


@dataclass(frozen=True)
class OAuthTTLs:
    """Token lifetimes, in seconds.

    Defaults: 1 hour access tokens, 30 day refresh tokens, 10 minute
    authorization codes and pending sessions — the same windows most
    single-user OAuth deployments use.
    """

    access_token: int = 3600
    refresh_token: int = 86400 * 30
    auth_code: int = 600
    pending_session: int = 600


class PratyabhijnaOAuthProvider:
    """OAuth 2.1 authorization server wrapping ``OAuthStorage``.

    Token and code values are ``secrets.token_urlsafe(32)`` — 256 bits
    of entropy, well above RFC 6749's 128-bit floor.
    """

    def __init__(
        self,
        storage: OAuthStorage,
        ttls: OAuthTTLs,
        login_url_base: str,
    ):
        self._storage = storage
        self._ttls = ttls
        self._login_url_base = login_url_base.rstrip("/")

    # -- clients ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        blob = await self._storage.load_client(client_id)
        if blob is None:
            return None
        return OAuthClientInformationFull.model_validate_json(blob)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The SDK's RegistrationHandler always sets client_id before calling us
        # (see register.py). An assertion here would be noise; trust the caller.
        await self._storage.save_client(
            client_info.client_id,
            client_info.model_dump_json(),
        )

    # -- authorize ----------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        session_id = secrets.token_urlsafe(32)
        await self._storage.save_pending({
            "session_id": session_id,
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "state": params.state,
            "resource": params.resource,
            "expires_at": time.time() + self._ttls.pending_session,
        })
        return f"{self._login_url_base}?session_id={session_id}"

    async def complete_pending_authorization(self, session_id: str) -> str | None:
        """Called by the login page after the user proves the secret.

        Mints an authorization code, deletes the pending record, and
        returns the client's redirect URL with ``code`` (and ``state``
        if the original request provided one). Returns None if the
        session is unknown or expired — the login page renders a
        friendly error in that case.
        """
        pending = await self._storage.load_pending(session_id)
        if pending is None:
            return None
        if pending["expires_at"] <= time.time():
            await self._storage.delete_pending(session_id)
            return None

        code = secrets.token_urlsafe(32)
        await self._storage.save_auth_code({
            "code": code,
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "code_challenge": pending["code_challenge"],
            "scopes": pending["scopes"],
            "resource": pending["resource"],
            "expires_at": time.time() + self._ttls.auth_code,
        })
        await self._storage.delete_pending(session_id)

        return construct_redirect_uri(
            pending["redirect_uri"],
            code=code,
            state=pending["state"],
        )

    # -- authorization codes ------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        row = await self._storage.load_auth_code(authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=row["code"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        access_token, refresh_token = await self._mint_token_pair(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
        )
        await self._storage.delete_auth_code(authorization_code.code)
        return _oauth_token(
            access=access_token,
            refresh=refresh_token,
            scopes=authorization_code.scopes,
            expires_in=self._ttls.access_token,
        )

    # -- refresh tokens -----------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        row = await self._storage.load_refresh_token(refresh_token)
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Token rotation: revoke old pair before minting the new one.
        await self._storage.delete_access_tokens_by_refresh(refresh_token.token)
        await self._storage.delete_refresh_token(refresh_token.token)

        new_scopes = scopes if scopes else refresh_token.scopes
        access, refresh = await self._mint_token_pair(
            client_id=client.client_id,
            scopes=new_scopes,
            resource=None,
        )
        return _oauth_token(
            access=access,
            refresh=refresh,
            scopes=new_scopes,
            expires_in=self._ttls.access_token,
        )

    # -- access tokens ------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self._storage.load_access_token(token)
        if row is None:
            return None
        expires_at = row["expires_at"]
        if expires_at is not None and expires_at <= time.time():
            return None
        return AccessToken(
            token=row["token"],
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=expires_at,
            resource=row["resource"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await self._storage.delete_access_token(token.token)
            return
        # Refresh: kill paired access tokens first so a stolen access token
        # can't outlive its refresh.
        await self._storage.delete_access_tokens_by_refresh(token.token)
        await self._storage.delete_refresh_token(token.token)

    # -- internals ----------------------------------------------------------

    async def _mint_token_pair(
        self,
        client_id: str,
        scopes: list[str],
        resource: str | None,
    ) -> tuple[str, str]:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        now = int(time.time())

        await self._storage.save_refresh_token({
            "token": refresh,
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + self._ttls.refresh_token,
        })
        await self._storage.save_access_token({
            "token": access,
            "client_id": client_id,
            "scopes": scopes,
            "resource": resource,
            "expires_at": now + self._ttls.access_token,
            "refresh_token": refresh,
        })
        return access, refresh


def _oauth_token(
    access: str,
    refresh: str,
    scopes: list[str],
    expires_in: int,
) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_in=expires_in,
        scope=" ".join(scopes) if scopes else None,
        refresh_token=refresh,
    )
