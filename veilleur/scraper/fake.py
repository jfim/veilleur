"""In-memory ``FakePassePartout`` for tests of downstream code.

Implements the ``Scraper`` Protocol but does not speak HTTP. Tests register
canned outcomes per URL: success, raise an error, or simulate a timeout.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from .client import FetchResult
from .errors import FetchError, FetchTimeout


@dataclass(slots=True)
class _Success:
    html: str
    status: int
    final_url: str
    elapsed_ms: int


@dataclass(slots=True)
class _Timeout:
    after: float
    partial: FetchResult | None


@dataclass(slots=True)
class _Error:
    error: FetchError


_Outcome = _Success | _Timeout | _Error


class FakePassePartout:
    """Test double for ``Scraper``. Honors the same Protocol as the real client."""

    def __init__(self) -> None:
        self._outcomes: dict[str, _Outcome] = {}
        self.calls: list[str] = []

    def register(
        self,
        url: str,
        *,
        html: str = "",
        status: int = 200,
        final_url: str | None = None,
        elapsed_ms: int = 5,
    ) -> None:
        """Register a successful fetch outcome for ``url``."""
        self._outcomes[url] = _Success(
            html=html,
            status=status,
            final_url=final_url or url,
            elapsed_ms=elapsed_ms,
        )

    def register_timeout(
        self,
        url: str,
        *,
        after: float = 0.0,
        partial: FetchResult | None = None,
    ) -> None:
        """Register a fetch that raises ``FetchTimeout``.

        ``after`` is an optional sleep (seconds) before raising — keep this
        small in tests; default 0 means raise immediately.
        """
        self._outcomes[url] = _Timeout(after=after, partial=partial)

    def register_error(self, url: str, error: FetchError) -> None:
        """Register a fetch that raises ``error``."""
        self._outcomes[url] = _Error(error=error)

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        outcome = self._outcomes.get(url)
        if outcome is None:
            raise KeyError(f"FakePassePartout has no registered outcome for {url!r}")
        if isinstance(outcome, _Timeout):
            if outcome.after > 0:
                await asyncio.sleep(outcome.after)
            raise FetchTimeout(f"fake timeout for {url}", partial=outcome.partial)
        if isinstance(outcome, _Error):
            raise outcome.error
        return FetchResult(
            html=outcome.html,
            final_url=outcome.final_url,
            status_code=outcome.status,
            fetched_at=datetime.now(UTC),
            elapsed_ms=outcome.elapsed_ms,
        )
