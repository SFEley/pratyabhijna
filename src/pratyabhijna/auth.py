"""Pratyabhijna bearer token authentication.

StaticTokenVerifier validates a single pre-shared API key.
This is the auth model for Claude Code access — the token lives
in .mcp.json on the client side and .env.prod on the server side.

For Claude.ai access (OAuth 2.0 flow), a full OAuthAuthorizationServerProvider
will replace this when needed.
"""

from mcp.server.auth.provider import AccessToken, TokenVerifier


class StaticTokenVerifier:
    """Validates a single static bearer token (pre-shared API key).

    Implements the TokenVerifier protocol. Token is compared with
    constant-time comparison to avoid timing attacks.
    """

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key

    async def verify_token(self, token: str) -> AccessToken | None:
        import hmac

        if not hmac.compare_digest(token, self._api_key):
            return None
        return AccessToken(token=token, client_id="claude-code", scopes=[])
