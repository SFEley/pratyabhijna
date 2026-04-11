"""Tests for the OAuth login page.

The Starlette route handlers in ``oauth/login.py`` are deliberately
thin — they're marked ``no cover`` and unpacked here through the pure
functions they wrap (``render_login_form``, ``render_error_page``,
``verify_password``, ``handle_login_submission``). Each is testable
without spinning up a full HTTP server.
"""

import pytest
from pydantic import AnyUrl

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from pratyabhijna.oauth.login import (
    LoginResult,
    handle_login_submission,
    render_error_page,
    render_login_form,
    verify_password,
)
from pratyabhijna.oauth.provider import OAuthTTLs, PratyabhijnaOAuthProvider
from pratyabhijna.oauth.storage import OAuthStorage


_PASSWORD = "s3cret-shared-between-us"


@pytest.fixture
async def provider(tmp_path):
    storage = OAuthStorage(db_path=str(tmp_path / "oauth.sqlite"))
    await storage.start()
    yield PratyabhijnaOAuthProvider(
        storage=storage,
        ttls=OAuthTTLs(
            access_token=3600,
            refresh_token=86400 * 30,
            auth_code=600,
            pending_session=600,
        ),
        login_url_base="https://vesper.example.com/login",
    )
    await storage.stop()


async def _seed_pending(provider) -> str:
    client = OAuthClientInformationFull(
        client_id="c1",
        redirect_uris=[AnyUrl("http://localhost:8080/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope="read",
    )
    await provider.register_client(client)
    url = await provider.authorize(
        client,
        AuthorizationParams(
            state="state-xyz",
            scopes=["read"],
            code_challenge="chall",
            redirect_uri=AnyUrl("http://localhost:8080/callback"),
            redirect_uri_provided_explicitly=True,
        ),
    )
    return url.split("session_id=")[1]


# ---------------------------------------------------------------------------
# render_login_form / render_error_page
# ---------------------------------------------------------------------------

class TestRenderLoginForm:
    def test_includes_session_id_as_hidden_input(self):
        html = render_login_form("abc123")
        assert 'name="session_id"' in html
        assert 'value="abc123"' in html

    def test_no_error_block_when_no_error(self):
        html = render_login_form("abc123")
        assert 'class="error"' not in html

    def test_error_block_rendered_when_given(self):
        html = render_login_form("abc123", error="Incorrect secret.")
        assert 'class="error"' in html
        assert "Incorrect secret." in html

    def test_html_escapes_session_id(self):
        html = render_login_form('"><script>alert(1)</script>')
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_escapes_error(self):
        html = render_login_form("s", error="<img src=x onerror=1>")
        assert "<img" not in html
        assert "&lt;img" in html


class TestRenderErrorPage:
    def test_message_is_escaped(self):
        html = render_error_page("<b>oops</b>")
        assert "<b>" not in html
        assert "&lt;b&gt;oops&lt;/b&gt;" in html

    def test_does_not_contain_login_form(self):
        html = render_error_page("expired")
        assert "<form" not in html


# ---------------------------------------------------------------------------
# verify_password
# ---------------------------------------------------------------------------

class TestVerifyPassword:
    def test_match_returns_true(self):
        assert verify_password("hunter2", "hunter2") is True

    def test_mismatch_returns_false(self):
        assert verify_password("hunter2", "wrong") is False

    def test_length_mismatch_returns_false(self):
        assert verify_password("hunter2", "hunter2x") is False

    def test_empty_strings(self):
        assert verify_password("", "") is True
        assert verify_password("s", "") is False


# ---------------------------------------------------------------------------
# handle_login_submission
# ---------------------------------------------------------------------------

class TestHandleLoginSubmission:
    async def test_success_returns_redirect(self, provider):
        session_id = await _seed_pending(provider)
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password=_PASSWORD,
        )
        assert result.redirect_url is not None
        assert result.redirect_url.startswith("http://localhost:8080/callback?")
        assert "code=" in result.redirect_url
        assert "state=state-xyz" in result.redirect_url
        assert result.form_error is None
        assert result.fatal_error is None

    async def test_wrong_password_returns_form_error(self, provider):
        session_id = await _seed_pending(provider)
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password="nope",
        )
        assert result.form_error == "Incorrect secret."
        assert result.redirect_url is None

    async def test_missing_password_returns_form_error(self, provider):
        session_id = await _seed_pending(provider)
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password=None,
        )
        assert result.form_error == "Please enter the secret."
        assert result.redirect_url is None

    async def test_missing_session_id_returns_fatal_error(self, provider):
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=None,
            submitted_password=_PASSWORD,
        )
        assert result.fatal_error is not None
        assert result.redirect_url is None

    async def test_unknown_session_returns_fatal_error(self, provider):
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id="nope",
            submitted_password=_PASSWORD,
        )
        assert result.fatal_error is not None
        assert result.redirect_url is None

    async def test_wrong_password_does_not_consume_session(self, provider):
        session_id = await _seed_pending(provider)
        await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password="nope",
        )
        # Correct password still works on retry.
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password=_PASSWORD,
        )
        assert result.redirect_url is not None

    async def test_success_consumes_session(self, provider):
        session_id = await _seed_pending(provider)
        await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password=_PASSWORD,
        )
        # Second attempt now fails because the pending record was deleted.
        result = await handle_login_submission(
            provider=provider,
            expected_password=_PASSWORD,
            session_id=session_id,
            submitted_password=_PASSWORD,
        )
        assert result.fatal_error is not None
