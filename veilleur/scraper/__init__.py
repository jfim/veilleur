"""Scraper module — async, single-flight passe-partout client.

Veilleur fetches every webpage through a passe-partout instance. This module
is pure-network: it knows nothing about the database or feed orchestration.

Caveat: Veilleur assumes exclusive use of its passe-partout. Running two
Veilleur processes (or any other client) against the same passe-partout
instance is unsupported in v1 — they will race for ``MAX_TABS`` and produce
inconsistent results.
"""

from __future__ import annotations

from typing import Protocol

from veilleur.config import get_settings

from .client import FetchResult, PassePartoutClient
from .errors import (
    FetchError,
    FetchTimeout,
    PassePartoutAuthError,
    PassePartoutBusy,
    PassePartoutProtocolError,
    PassePartoutUnavailable,
    UnsupportedContentType,
)
from .fake import FakePassePartout


class Scraper(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


def client_from_env() -> PassePartoutClient:
    """Construct a ``PassePartoutClient`` from process settings.

    Raises ``RuntimeError`` (not ``FetchError``) if ``PASSEPARTOUT_URL`` is
    unset — that is a configuration bug, not a runtime fetch failure.
    """
    settings = get_settings()
    base_url = settings.PASSEPARTOUT_URL
    if not base_url:
        raise RuntimeError("PASSEPARTOUT_URL is not configured")
    token = (
        settings.PASSEPARTOUT_BEARER_TOKEN.get_secret_value()
        if settings.PASSEPARTOUT_BEARER_TOKEN
        else None
    )
    return PassePartoutClient(base_url=base_url, bearer_token=token)


__all__ = [
    "FakePassePartout",
    "FetchError",
    "FetchResult",
    "FetchTimeout",
    "PassePartoutAuthError",
    "PassePartoutBusy",
    "PassePartoutClient",
    "PassePartoutProtocolError",
    "PassePartoutUnavailable",
    "Scraper",
    "UnsupportedContentType",
    "client_from_env",
]
