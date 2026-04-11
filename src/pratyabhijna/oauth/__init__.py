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
"""
