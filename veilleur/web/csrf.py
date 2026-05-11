"""Double-submit CSRF protection for the cookie-authenticated UI.

Every response carries a ``veilleur_csrf`` cookie holding a per-browser
token (rotated only on first contact). State-changing requests must echo
that token back, either via a hidden ``csrf_token`` form field or via
the ``X-CSRF-Token`` header. The dependency rejects any request whose
echo doesn't match the cookie under constant-time comparison.

The cookie is HttpOnly + SameSite=Strict — templates read the value via
``request.state.csrf_token`` (set by the middleware) rather than from
JavaScript, so HttpOnly doesn't get in the way and the Strict policy
keeps the token from leaking via top-level navigation tricks that
``Lax`` would otherwise allow.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse
from starlette.types import ASGIApp

CSRF_COOKIE_NAME = "veilleur_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

#: Methods that are *defined* as state-changing per RFC 9110. GET / HEAD /
#: OPTIONS / TRACE are explicitly safe and bypass CSRF validation.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


class CSRFCookieMiddleware(BaseHTTPMiddleware):
    """Ensure every response carries a CSRF cookie and expose it on request.state.

    On entry: read the cookie. If absent, mint a fresh token and stash it
    on ``request.state`` so downstream template rendering can embed it.
    On exit: emit ``Set-Cookie`` only when we minted a new value, so
    repeated requests don't rotate the token (which would invalidate
    in-flight forms a user has open in another tab).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(  # type: ignore[override]
        self,
        request: StarletteRequest,
        call_next: object,
    ) -> StarletteResponse:
        existing = request.cookies.get(CSRF_COOKIE_NAME)
        token = existing or _generate_token()
        request.state.csrf_token = token
        response: StarletteResponse = await call_next(request)  # type: ignore[misc,operator]
        if existing is None:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=token,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                path="/",
            )
        return response


async def require_csrf_token(request: Request) -> None:
    """Reject state-changing requests whose echoed token doesn't match the cookie.

    Accepts the echo either as a form field (``csrf_token``) or as an
    ``X-CSRF-Token`` header so JSON-style callers don't have to forge a
    multipart body. Safe methods are exempt.
    """
    if request.method.upper() not in _UNSAFE_METHODS:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF cookie missing",
        )
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if header_token and hmac.compare_digest(header_token, cookie_token):
        return
    # Falling back to the form body re-reads it; Starlette caches the
    # parsed FormData so the route handler's Form(...) parameters still see
    # the same values.
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()
        form_token = form.get(CSRF_FORM_FIELD)
        if isinstance(form_token, str) and hmac.compare_digest(form_token, cookie_token):
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CSRF token mismatch",
    )
