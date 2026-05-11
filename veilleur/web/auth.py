"""Cookie-based session auth for the web UI.

The web UI shares its credential with the REST API: ``API_BEARER_TOKEN``
is the password. The login form (``GET /login``) accepts that token as a
single password field; the POST handler verifies it with a constant-time
comparison and sets a ``veilleur_session`` cookie containing the token.
Subsequent requests are authorised by the :func:`require_session_cookie`
dependency, which redirects unauthenticated browsers to ``/login``
(preserving the original path via ``?next=``).

The REST API still uses header-based bearer-token auth — see
:mod:`veilleur.api.auth`. This module only covers the human-facing UI.
"""

from __future__ import annotations

import hmac
from urllib.parse import quote

from fastapi import HTTPException, Request, status

from veilleur.config import get_settings

COOKIE_NAME = "veilleur_session"
# 30 days, matching the "remember me" wording in the login template.
REMEMBER_ME_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def is_safe_next(target: str) -> bool:
    """True when ``target`` is a same-origin path we can safely redirect to.

    Rejects scheme-relative (``//evil.example``) and absolute URLs so a
    crafted ``?next=`` can't turn the login page into an open redirector.
    """
    return target.startswith("/") and not target.startswith("//")


def _login_location(request: Request) -> str:
    current = request.url.path
    if request.url.query:
        current = f"{current}?{request.url.query}"
    if not current or current == "/login":
        return "/login"
    return f"/login?next={quote(current, safe='/')}"


async def require_session_cookie(request: Request) -> None:
    """Authorise the request via the session cookie, or redirect to /login.

    Unauthenticated requests get a 303 redirect rather than a 401 so
    browsers land somewhere useful. When auth is disabled
    (``API_AUTH_DISABLED=true``) the dependency is a no-op.
    """
    settings = get_settings()
    if settings.API_AUTH_DISABLED:
        return
    expected = settings.API_BEARER_TOKEN
    if expected is None or expected.get_secret_value() == "":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured",
        )
    presented = request.cookies.get(COOKIE_NAME)
    if not presented or not hmac.compare_digest(presented, expected.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="login required",
            headers={"Location": _login_location(request)},
        )


def verify_password(password: str) -> bool:
    """Constant-time check of a login-form password against the configured token."""
    settings = get_settings()
    expected = settings.API_BEARER_TOKEN
    if expected is None or expected.get_secret_value() == "":
        return False
    return hmac.compare_digest(password, expected.get_secret_value())
