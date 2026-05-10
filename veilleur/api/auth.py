"""Bearer-token authentication for the REST API.

Per ``RESOLUTIONS.md``: when ``API_BEARER_TOKEN`` is unset the API
rejects every programmatic request. ``/healthz`` is wired separately (it is
not behind this dependency).
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from veilleur.config import get_settings


async def require_bearer_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> None:
    """FastAPI dependency that enforces ``Authorization: Bearer <token>``.

    Raises 401 when the configured token is unset, when the header is
    missing or malformed, or when the supplied token does not match.
    """
    expected = get_settings().API_BEARER_TOKEN
    if expected is None or expected.get_secret_value() == "":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API authentication is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization.split(" ", 1)[1].strip()
    if not hmac.compare_digest(presented, expected.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
