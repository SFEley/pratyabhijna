"""Password login page for the OAuth authorization server.

Pratyabhijna is a single-user deployment, so "login" is simply proving
knowledge of the shared secret stored in ``config.api_key``. The page
is rendered server-side as a tiny HTML form:

1. OAuth client hits ``/authorize`` → provider creates a pending
   session and redirects the user here with ``?session_id=...``.
2. User types the secret and submits.
3. If the secret matches (constant-time compare), the provider mints
   an authorization code and we redirect back to the client's
   ``redirect_uri`` with ``?code=...&state=...``.

No cookies, no CSRF tokens, no JavaScript. The session handle is
opaque and short-lived (10 minutes), and the secret check is gated on
it, so there's nothing to persist across requests.

This module factors the work into four pure functions
(``render_login_form``, ``render_error_page``, ``verify_password``,
``handle_login_submission``) that are unit-tested directly, plus
``register_login_routes`` which wires them into a FastMCP instance as
``GET /login`` and ``POST /login``.
"""

import hmac
import html
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from pratyabhijna.log import get_logger
from pratyabhijna.oauth.provider import PratyabhijnaOAuthProvider

_log = get_logger(__name__)


_FORM_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pratyabhijna — Sign in</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 28rem;
          margin: 4rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.25rem; font-weight: 600; margin-bottom: 0.25rem; }}
  p  {{ color: #666; font-size: 0.9rem; }}
  label {{ display: block; margin-top: 1rem; font-size: 0.9rem; }}
  input[type=password] {{ width: 100%; padding: 0.5rem; font-size: 1rem;
                          border: 1px solid #ccc; border-radius: 4px; }}
  button {{ margin-top: 1rem; padding: 0.5rem 1rem; font-size: 1rem;
            border: 0; border-radius: 4px; background: #222; color: #fff;
            cursor: pointer; }}
  .error {{ margin-top: 1rem; padding: 0.5rem 0.75rem; background: #fee;
            border: 1px solid #f99; border-radius: 4px; color: #900;
            font-size: 0.9rem; }}
</style>
</head>
<body>
  <h1>Pratyabhijna</h1>
  <p>Sign in to authorize the requesting client.</p>
  {error_html}
  <form method="post" action="/login">
    <input type="hidden" name="session_id" value="{session_id}">
    <label>Secret
      <input type="password" name="password" autofocus required>
    </label>
    <button type="submit">Sign in</button>
  </form>
</body>
</html>
"""


_ERROR_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pratyabhijna — Error</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 28rem;
          margin: 4rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.25rem; font-weight: 600; }}
  p  {{ color: #666; }}
</style>
</head>
<body>
  <h1>Pratyabhijna</h1>
  <p>{message}</p>
</body>
</html>
"""


@dataclass(frozen=True)
class LoginResult:
    """What to do after a POST /login submission.

    Exactly one of ``redirect_url`` (success — send the user back to
    the OAuth client) or ``form_error`` (re-render the form with this
    error) or ``fatal_error`` (render the error page) is populated.
    """

    redirect_url: str | None = None
    form_error: str | None = None
    fatal_error: str | None = None


def render_login_form(session_id: str, error: str | None = None) -> str:
    """Render the sign-in HTML for a given session.

    ``session_id`` is inserted into a hidden input so the POST handler
    can look it up. Both it and the error message are HTML-escaped.
    """
    error_html = (
        f'<div class="error">{html.escape(error)}</div>' if error else ""
    )
    return _FORM_TEMPLATE.format(
        session_id=html.escape(session_id),
        error_html=error_html,
    )


def render_error_page(message: str) -> str:
    """Render a terminal error page (no form, no retry)."""
    return _ERROR_TEMPLATE.format(message=html.escape(message))


def verify_password(expected: str, submitted: str) -> bool:
    """Constant-time comparison of the shared secret.

    ``hmac.compare_digest`` short-circuits to False on length
    mismatch in constant time, which is fine — a length oracle on a
    single-user secret gives an attacker essentially nothing.
    """
    return hmac.compare_digest(expected.encode(), submitted.encode())


async def handle_login_submission(
    provider: PratyabhijnaOAuthProvider,
    expected_password: str,
    session_id: str | None,
    submitted_password: str | None,
) -> LoginResult:
    """Process a POST /login submission.

    Returns a ``LoginResult`` describing what the HTTP layer should
    do next. Does not touch Starlette — the caller is responsible for
    turning the result into a response.
    """
    if not session_id:
        return LoginResult(fatal_error="Missing session identifier.")
    if not submitted_password:
        return LoginResult(form_error="Please enter the secret.")

    if not verify_password(expected_password, submitted_password):
        _log.info("oauth login failure session=%s", session_id)
        return LoginResult(form_error="Incorrect secret.")

    redirect = await provider.complete_pending_authorization(session_id)
    if redirect is None:
        return LoginResult(
            fatal_error=(
                "This sign-in request has expired or is no longer valid. "
                "Please start again from the client application."
            ),
        )
    _log.info("oauth login success session=%s", session_id)
    return LoginResult(redirect_url=redirect)


def register_login_routes(
    server,
    provider: PratyabhijnaOAuthProvider,
    expected_password: str,
) -> None:
    """Register GET /login and POST /login on a FastMCP server.

    These routes are declared with ``server.custom_route``, which
    explicitly bypasses bearer-token auth — a requirement, since the
    user hasn't proven anything yet when they land here.
    """

    @server.custom_route("/login", methods=["GET"])
    async def login_get(request: Request) -> Response:  # pragma: no cover — thin wrapper
        session_id = request.query_params.get("session_id")
        if not session_id:
            return HTMLResponse(
                render_error_page("Missing session identifier."),
                status_code=400,
            )
        return HTMLResponse(render_login_form(session_id))

    @server.custom_route("/login", methods=["POST"])
    async def login_post(request: Request) -> Response:  # pragma: no cover — thin wrapper
        form = await request.form()
        session_id = form.get("session_id")
        submitted = form.get("password")
        result = await handle_login_submission(
            provider=provider,
            expected_password=expected_password,
            session_id=session_id,
            submitted_password=submitted,
        )
        if result.redirect_url is not None:
            return RedirectResponse(result.redirect_url, status_code=303)
        if result.fatal_error is not None:
            return HTMLResponse(
                render_error_page(result.fatal_error),
                status_code=400,
            )
        # form_error — re-render with the message
        return HTMLResponse(
            render_login_form(session_id or "", error=result.form_error),
            status_code=401,
        )
