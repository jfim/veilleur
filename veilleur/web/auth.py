"""Cookie-based session auth for the web UI.

The web UI shares its credential with the REST API: ``API_BEARER_TOKEN``
is the password. The login form (``GET /login``) accepts that token as a
single password field; the POST handler verifies it with a constant-time
comparison and issues a signed session token (via :mod:`itsdangerous`)
in the ``veilleur_session`` cookie. The cookie value is *not* the
bearer token itself, so log capture / browser-extension exfil of the
cookie does not hand over the long-lived API credential — only a
session marker bound to the configured signing secret and an expiry.

Subsequent requests are authorised by :func:`require_session_cookie`,
which redirects unauthenticated browsers to ``/login`` (preserving the
original path via ``?next=``).

The REST API still uses header-based bearer-token auth — see
:mod:`veilleur.api.auth`. This module only covers the human-facing UI.
"""

from __future__ import annotations

import hashlib
import hmac
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from veilleur.config import get_settings

COOKIE_NAME = "veilleur_session"
# 30 days, matching the "remember me" wording in the login template.
REMEMBER_ME_MAX_AGE_SECONDS = 60 * 60 * 24 * 30

#: ``itsdangerous`` salt namespace. Distinct from the signing secret —
#: keeps any future signed payloads (e.g. password-reset URLs) from
#: colliding even when they share the same secret.
_SESSION_SALT = "veilleur.session.v1"


def _derive_session_secret() -> str:
    """Return the secret used to sign session tokens.

    Prefers an explicit ``SESSION_SECRET`` setting when present; falls back
    to a token derived from ``API_BEARER_TOKEN`` so a single-replica
    deployment doesn't need to set both env vars. The fallback is hashed
    rather than used verbatim so the on-disk signing key never trivially
    encodes the bearer credential.
    """
    settings = get_settings()
    explicit = settings.SESSION_SECRET
    if explicit is not None and explicit.get_secret_value() != "":
        return explicit.get_secret_value()
    bearer = settings.API_BEARER_TOKEN
    if bearer is None or bearer.get_secret_value() == "":
        # require_session_cookie surfaces a clearer 503 in this case; we
        # still need *some* string here so itsdangerous doesn't raise on
        # construction during health probes etc.
        return "veilleur-no-session-secret-configured"
    return hashlib.sha256(
        ("veilleur-session-derive-v1:" + bearer.get_secret_value()).encode("utf-8")
    ).hexdigest()


@lru_cache(maxsize=1)
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_derive_session_secret(), salt=_SESSION_SALT)


def reset_serializer_cache() -> None:
    """Drop the memoised serializer.

    Tests that swap the bearer token / session secret mid-run need to
    rebuild the serializer; production never calls this.
    """
    _serializer.cache_clear()


def issue_session_token() -> str:
    """Mint a fresh signed session token to put in the ``veilleur_session`` cookie."""
    payload: dict[str, Any] = {"v": 1}
    return _serializer().dumps(payload)


def _verify_session_token(token: str) -> bool:
    try:
        _serializer().loads(token, max_age=REMEMBER_ME_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return True


def is_safe_next(target: str) -> bool:
    """True when ``target`` is a same-origin path we can safely redirect to.

    Rejects scheme-relative (``//evil.example``), backslash-prefixed
    (``/\\evil.example`` — historically normalised to ``//`` by some
    browsers) and absolute URLs so a crafted ``?next=`` can't turn the
    login page into an open redirector.
    """
    if not target.startswith("/"):
        return False
    if len(target) >= 2 and target[1] in ("/", "\\"):
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


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
    if not presented or not _verify_session_token(presented):
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
