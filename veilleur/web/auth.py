"""HTTP Basic auth for the Phase 9 web UI.

Per ``docs/design/phases-6-9.md``, the web UI shares its credential with the
REST API: ``VEILLEUR_API_BEARER_TOKEN`` doubles as the HTTP Basic password.
The username is ignored (any non-empty value is accepted) — operators only
ever type the password into the browser prompt.

When the token is unset the dependency returns ``401`` with a
``WWW-Authenticate: Basic`` challenge so browsers prompt for credentials.
"""

from __future__ import annotations

import base64
import binascii
import hmac

from fastapi import Header, HTTPException, status

from veilleur.config import get_settings

_REALM = 'Veilleur'
_CHALLENGE = f'Basic realm="{_REALM}"'


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": _CHALLENGE},
    )


async def require_basic_auth(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """Enforce HTTP Basic with the bearer token as the password."""
    expected = get_settings().VEILLEUR_API_BEARER_TOKEN
    if expected is None or expected.get_secret_value() == "":
        raise _unauthorized("API authentication is not configured")
    if not authorization or not authorization.lower().startswith("basic "):
        raise _unauthorized("authentication required")
    encoded = authorization.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _unauthorized("malformed credentials") from exc
    if ":" not in decoded:
        raise _unauthorized("malformed credentials")
    _user, password = decoded.split(":", 1)
    if not hmac.compare_digest(password, expected.get_secret_value()):
        raise _unauthorized("invalid credentials")
