"""Real ``PassePartoutClient`` — async, single-flight, httpx-backed.

The client drives passe-partout's stateful tab API:

1. ``POST /tabs`` (with ``{"url": ...}``) creates a tab and starts navigation.
2. ``POST /tabs/{id}/wait`` (with ``network_idle: true``) blocks until idle.
3. ``GET  /tabs/{id}/html`` reads the rendered HTML.
4. ``DELETE /tabs/{id}`` cleans up (best-effort, in ``finally``).

The wait timeout we send to passe-partout is ``wait_timeout`` (default 30s).
The overall local wall-clock budget is ``wait_timeout + max(0.2 * wait_timeout, 10)``
so a remote networkidle timeout reaches us before we cancel locally; this
matters because a local cancel produces only a best-effort partial, while a
remote timeout returns gracefully and lets us read the final HTML. A
per-instance ``asyncio.Lock`` ensures at most one in-flight fetch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from types import TracebackType
from typing import Any

import httpx

from .errors import (
    FetchError,
    FetchTimeout,
    PassePartoutAuthError,
    PassePartoutBusy,
    PassePartoutProtocolError,
    PassePartoutUnavailable,
    UnsupportedContentType,
)

logger = logging.getLogger(__name__)


_ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a successful (or partially successful) fetch.

    Target-site HTTP errors live in ``status_code`` — they are not raised.
    """

    html: str
    final_url: str
    status_code: int
    fetched_at: datetime
    elapsed_ms: int


def _content_type_ok(ctype: str) -> bool:
    base = ctype.split(";", 1)[0].strip().lower()
    return base in _ALLOWED_CONTENT_TYPES


def _map_status(response: httpx.Response) -> None:
    """Map a passe-partout (not target site) HTTP status to our exceptions."""
    sc = response.status_code
    if sc < 400:
        return
    if sc in (401, 403):
        raise PassePartoutAuthError(f"passe-partout auth failed: HTTP {sc}")
    if sc == 429:
        raise PassePartoutBusy("passe-partout returned 429 (MAX_TABS)")
    if 500 <= sc < 600:
        raise PassePartoutUnavailable(f"passe-partout HTTP {sc}")
    raise PassePartoutProtocolError(
        f"unexpected passe-partout HTTP {sc}: {response.text[:200]!r}"
    )


class PassePartoutClient:
    """Async, single-flight client for passe-partout's stateful tab API."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str | None = None,
        *,
        wait_timeout: float = 30.0,
        local_budget: float | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url
        self._bearer_token = bearer_token or None
        self._wait_timeout = wait_timeout
        self._local_budget = (
            local_budget
            if local_budget is not None
            else wait_timeout + max(0.2 * wait_timeout, 10.0)
        )
        self._lock = asyncio.Lock()
        self._owns_client = http_client is None
        headers: dict[str, str] = {}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        if http_client is None:
            self._http: httpx.AsyncClient = httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                # Read timeout matches local_budget so the asyncio.timeout below
                # fires first and produces the clearer "exceeded budget" error
                # rather than a bare httpx.ReadTimeout from a long /wait call.
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=self._local_budget,
                    write=5.0,
                    pool=5.0,
                ),
            )
        else:
            self._http = http_client

    async def __aenter__(self) -> PassePartoutClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def fetch(self, url: str) -> FetchResult:
        """Fetch ``url`` via passe-partout and return rendered HTML.

        Single-flight: at most one in-flight fetch per instance.
        Total wall-clock budget: ``self._local_budget`` (wait timeout plus slack).
        """
        async with self._lock:
            # State the outer timeout handler needs in order to attempt the
            # best-effort partial-HTML grab on a cancelled fetch.
            state: dict[str, object] = {
                "started": monotonic(),
                "tab_id": None,
                "status_code": 0,
                "final_url": url,
                "step": "create_tab",
            }
            try:
                async with asyncio.timeout(self._local_budget):
                    return await self._fetch_locked(url, state)
            except TimeoutError as exc:
                tab_id = state["tab_id"]
                tab_id_str = tab_id if isinstance(tab_id, str) else None
                started = state["started"]
                started_f = started if isinstance(started, float) else monotonic()
                status_code = state["status_code"]
                status_code_i = status_code if isinstance(status_code, int) else 0
                final_url = state["final_url"]
                final_url_s = final_url if isinstance(final_url, str) else url
                partial = await self._best_effort_partial(
                    tab_id_str, started_f, status_code_i, final_url_s
                )
                raise FetchTimeout(
                    f"fetch exceeded {self._local_budget}s budget", partial=partial
                ) from exc

    async def _fetch_locked(
        self, url: str, state: dict[str, object]
    ) -> FetchResult:
        tab_id: str | None = None

        try:
            state["step"] = "create_tab"
            create_resp = await self._http.post("/tabs", json={"url": url})
            tab_id, status_code, final_url, content_type = self._parse_create(
                create_resp, url
            )
            state["tab_id"] = tab_id
            state["status_code"] = status_code
            state["final_url"] = final_url

            if not _content_type_ok(content_type):
                raise UnsupportedContentType(content_type)

            started = state["started"]
            started_f = started if isinstance(started, float) else monotonic()
            state["step"] = "wait_networkidle"
            wait_resp = await self._http.post(
                f"/tabs/{tab_id}/wait",
                json={"network_idle": True, "timeout_ms": int(self._wait_timeout * 1000)},
            )
            self._handle_tab_response(wait_resp)

            state["step"] = "fetch_html"
            html_resp = await self._http.get(f"/tabs/{tab_id}/html")
            self._handle_tab_response(html_resp)
            html = html_resp.text

            elapsed_ms = int((monotonic() - started_f) * 1000)
            return FetchResult(
                html=html,
                final_url=final_url,
                status_code=status_code,
                fetched_at=datetime.now(UTC),
                elapsed_ms=elapsed_ms,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            step = state.get("step", "?")
            raise PassePartoutUnavailable(
                f"passe-partout connection error during {step}: {exc}"
            ) from exc
        except httpx.ReadTimeout as exc:
            step = state.get("step", "?")
            raise FetchTimeout(
                f"passe-partout read timeout during {step} "
                f"(httpx read={self._local_budget}s): {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            step = state.get("step", "?")
            raise PassePartoutUnavailable(
                f"passe-partout http error during {step}: {exc}"
            ) from exc
        finally:
            if tab_id is not None:
                # Shielded so a timeout cancellation doesn't kill the cleanup.
                await asyncio.shield(self._safe_delete(tab_id))

    def _parse_create(
        self, response: httpx.Response, requested_url: str
    ) -> tuple[str, int, str, str]:
        """Parse ``POST /tabs`` response into (tab_id, status, final_url, ctype)."""
        _map_status(response)
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PassePartoutProtocolError(
                f"invalid JSON from POST /tabs: {response.text[:200]!r}"
            ) from exc
        raw_id = body.get("id")
        if isinstance(raw_id, int):
            tab_id = str(raw_id)
        elif isinstance(raw_id, str) and raw_id:
            tab_id = raw_id
        else:
            raise PassePartoutProtocolError(
                f"missing/invalid 'id' in POST /tabs response: {body!r}"
            )
        status_code = body.get("status")
        if not isinstance(status_code, int):
            raise PassePartoutProtocolError(
                f"missing/invalid 'status' in POST /tabs response: {body!r}"
            )
        final_url = body.get("final_url")
        if not isinstance(final_url, str) or not final_url:
            final_url = requested_url
        content_type = body.get("content_type")
        if not isinstance(content_type, str):
            content_type = ""
        return tab_id, status_code, final_url, content_type

    def _handle_tab_response(self, response: httpx.Response) -> None:
        """Map 4xx/5xx on a per-tab endpoint to our exception taxonomy."""
        if response.status_code == 404:
            raise PassePartoutProtocolError(
                f"tab not found (lost): HTTP 404 on {response.request.url}"
            )
        _map_status(response)

    async def _best_effort_partial(
        self,
        tab_id: str | None,
        started: float,
        status_code: int,
        final_url: str,
    ) -> FetchResult | None:
        """Try one last GET /html (under a 2s shield) for the timeout path.

        Wrapped in ``asyncio.shield`` so it survives the outer-budget
        cancellation that put us on the timeout path in the first place.
        """
        if tab_id is None:
            return None
        try:
            resp = await asyncio.shield(
                asyncio.wait_for(
                    self._http.get(f"/tabs/{tab_id}/html"), timeout=2.0
                )
            )
        except (TimeoutError, httpx.HTTPError) as exc:
            logger.warning("best-effort partial-html fetch failed: %s", exc)
            return None
        if resp.status_code >= 400:
            return None
        elapsed_ms = int((monotonic() - started) * 1000)
        return FetchResult(
            html=resp.text,
            final_url=final_url,
            status_code=status_code,
            fetched_at=datetime.now(UTC),
            elapsed_ms=elapsed_ms,
        )

    async def _safe_delete(self, tab_id: str) -> None:
        """Best-effort tab cleanup. Logs but never raises."""
        try:
            async with asyncio.timeout(5.0):
                await self._http.delete(f"/tabs/{tab_id}")
        except (TimeoutError, httpx.HTTPError, FetchError) as exc:
            logger.warning("failed to delete passe-partout tab %s: %s", tab_id, exc)
