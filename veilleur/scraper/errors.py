"""Error taxonomy for the scraper module.

These exceptions concern the *operation* of fetching via passe-partout (the
network between us and it, the wait budget, passe-partout's own behavior).

Target-site HTTP errors (e.g. the page itself returns 404 or 500) are NOT
raised — they come back as ``status_code`` on the ``FetchResult``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import FetchResult


class FetchError(Exception):
    """Base class for all scraper errors. Never raised directly."""


class FetchTimeout(FetchError):
    """The 30s overall fetch budget expired before HTML was read.

    Carries a ``partial`` ``FetchResult`` when one could be salvaged from
    a best-effort ``GET /tabs/{id}/html`` after the timeout fired.
    """

    def __init__(self, message: str, *, partial: FetchResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial


class PassePartoutUnavailable(FetchError):
    """passe-partout is unreachable or returned a 5xx / network error."""


class PassePartoutAuthError(FetchError):
    """passe-partout returned 401/403 (bad or missing bearer token)."""


class PassePartoutBusy(FetchError):
    """passe-partout returned 429 (``MAX_TABS`` reached)."""


class PassePartoutProtocolError(FetchError):
    """passe-partout returned an unexpected response shape or status."""


class UnsupportedContentType(FetchError):
    """The target page's Content-Type was not text/html or application/xhtml+xml."""

    def __init__(self, content_type: str) -> None:
        super().__init__(
            f"server returned {content_type or '<missing>'} instead of text/html"
        )
        self.content_type = content_type
