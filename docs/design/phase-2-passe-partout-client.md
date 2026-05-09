# Phase 2 — Passe-partout Client

A thin async HTTP wrapper around [passe-partout](https://github.com/jfim/passe-partout)
that fetches a single URL through the stateful tab flow, waits for `networkidle`,
and returns the rendered HTML. Lives in `veilleur/scraper/`.

## Goals

- Async `Scraper.fetch(url) -> FetchResult` that returns rendered HTML for one URL.
- Drive passe-partout's stateful tab API: create tab, wait for network idle, read
  HTML, close tab. Fresh tab per fetch.
- Single-flight: at most one in-flight fetch per process via `asyncio.Lock`.
- Honor `PASSEPARTOUT_URL` and `PASSEPARTOUT_BEARER_TOKEN` env vars; always send
  `Authorization: Bearer …` when the token is set.
- Surface failures with a clean, narrow exception taxonomy. Higher layers decide
  whether to retry on the next poll cycle.
- Be testable without a live passe-partout: ship a `FakePassePartout`
  implementation and HTTP-level transcript fixtures.

## Non-goals (v1)

- **No retries.** A failure returns to the caller; the next scheduled poll
  retries naturally.
- **No cookie persistence.** Each tab starts cookieless. Tracked as a follow-up.
- **No screenshots, console logs, or resource capture.** Tracked as follow-ups.
- **No parallelism.** Single-flight is enforced and documented.
- **No multi-instance coordination.** Two Veilleur processes against one
  passe-partout will race; users must not do this. Documented caveat only.
- **No authentication flows, no clicks, no JS evaluation.** Read-only fetch.

## Public API

```python
# veilleur/scraper/__init__.py
from typing import Protocol
from .errors import (
    FetchError,
    FetchTimeout,
    PassePartoutUnavailable,
    PassePartoutAuthError,
    PassePartoutBusy,
)
from .client import FetchResult, PassePartoutClient
from .fake import FakePassePartout

class Scraper(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
```

`PassePartoutClient` and `FakePassePartout` both satisfy `Scraper`.

### `FetchResult`

```python
@dataclass(frozen=True, slots=True)
class FetchResult:
    html: str                 # rendered HTML at the moment of read
    final_url: str            # URL after redirects (from passe-partout)
    status_code: int          # main-document HTTP status from the target site
    fetched_at: datetime      # UTC, set when the tab finished loading
    elapsed_ms: int           # wall time from tab create to html read
```

Target-site HTTP errors (e.g. 404, 500) are **not** raised — they come back in
`status_code`. The caller decides how to interpret a non-2xx page.

### Error taxonomy

All errors inherit from `FetchError`. They concern the *operation* (talking to
passe-partout, the network between us and it, the wait budget). Target-site
status codes are values in `FetchResult`, not exceptions.

| Class                    | Raised when                                                                 |
| ------------------------ | --------------------------------------------------------------------------- |
| `FetchError`             | Base class. Never raised directly.                                          |
| `FetchTimeout`           | The 30 s overall budget expired before HTML was read.                       |
| `PassePartoutUnavailable`| Connection refused, DNS failure, 5xx from passe-partout itself, 503/504.    |
| `PassePartoutAuthError`  | passe-partout returned 401/403 (bad or missing bearer token).               |
| `PassePartoutBusy`       | passe-partout returned 429 (`MAX_TABS` reached).                            |
| `PassePartoutProtocolError` | Unexpected response shape, malformed JSON, missing fields.               |

`FetchTimeout` returns the most recent partial state when possible (we always
attempt `GET /tabs/{id}/html` once before giving up). The exception carries
`partial: FetchResult | None` and a warning is logged. This is the "hard timeout
returns whatever HTML is current" behavior.

## Passe-partout endpoint flow

Confirmed from the [passe-partout README](https://github.com/jfim/passe-partout).
Per fetch, the client executes:

1. **`POST /tabs`** with `{"url": <target_url>}`
   → `{id, status, final_url, content_type}`.
   `status` is the target site's main-document HTTP status (already followed
   redirects). passe-partout has already started the navigation; we record
   `status_code` and `final_url` from this response.

2. **`POST /tabs/{id}/wait`** with `{"network_idle": true, "timeout_ms": <budget>}`.
   This is passe-partout's native network-idle wait. We pass the remaining
   budget so the server-side wait is bounded too; if it returns before our local
   deadline, great.

3. **`GET /tabs/{id}/html`** → response body is the current rendered HTML.

4. **`DELETE /tabs/{id}`** — always attempted, even on error paths, in
   `try/finally`. Failures here are logged but not raised (the tab will be
   reaped by `IDLE_TAB_CLOSE_SECONDS` anyway).

### Authorization

If `PASSEPARTOUT_BEARER_TOKEN` is set, every request carries
`Authorization: Bearer <token>`. If unset, no header is sent. The token is
plumbed at client construction time, not per-call.

### HTTP layer

`httpx.AsyncClient` with:
- `base_url=PASSEPARTOUT_URL`
- `timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)`
- One client per `PassePartoutClient` instance, lifecycle-managed via
  `__aenter__/__aexit__` (and explicit `aclose()`).

Status mapping for passe-partout responses (not the target site):
- 401 / 403 → `PassePartoutAuthError`
- 404 on `/tabs/{id}` (lost tab) → `PassePartoutProtocolError`
- 429 → `PassePartoutBusy`
- 5xx → `PassePartoutUnavailable`
- network errors (`httpx.ConnectError`, `ReadError`, etc.) → `PassePartoutUnavailable`

## Single-flight

A module-level (instance-scoped) `asyncio.Lock` wraps the entire `fetch`
coroutine body, including tab cleanup. Acquiring the lock is *inside* the
30 s budget — if a previous fetch is in flight, the new one will time out
just like any other slow path.

```python
async def fetch(self, url: str) -> FetchResult:
    async with self._lock:
        return await self._fetch_locked(url)
```

Caveat documented in module docstring and README:

> Veilleur assumes exclusive use of its passe-partout. Running two Veilleur
> processes (or any other client) against the same passe-partout instance is
> unsupported in v1 — they will race for `MAX_TABS` and produce inconsistent
> results.

## 30 s timeout budget

A single `asyncio.timeout(30.0)` wraps the whole `_fetch_locked` body. There is
no per-step micro-budgeting beyond this. The internal allocation is illustrative:

| Step                    | Soft target | Notes                                           |
| ----------------------- | ----------- | ----------------------------------------------- |
| `POST /tabs`            | ≤ 10 s      | Includes target-site initial load + redirects.  |
| `POST /tabs/{id}/wait`  | remaining   | We pass `timeout_ms = max(1, remaining_ms)`.    |
| `GET /tabs/{id}/html`   | ≤ 2 s       | Local; only slow if the HTML is huge.           |
| `DELETE /tabs/{id}`     | best effort | Runs in `finally` with its own 5 s shield.      |

On `asyncio.TimeoutError` we attempt one last best-effort
`GET /tabs/{id}/html` (under a 2 s shield) so the caller can inspect whatever
loaded. If that succeeds, we raise `FetchTimeout(partial=FetchResult(...))`.

## FakePassePartout

`veilleur/scraper/fake.py` ships an in-memory implementation of `Scraper` that
satisfies the same Protocol but **does not** speak HTTP — it returns canned
results. Construction:

```python
fake = FakePassePartout()
fake.register("https://example.com", html="<html>…</html>", status=200)
fake.register_timeout("https://slow.example", after=29.5)
fake.register_error("https://502.example", PassePartoutUnavailable("upstream"))
fake.register("https://404.example", html="<h1>not found</h1>", status=404)
```

Behaviors it can simulate:
- Successful fetch with arbitrary `(html, status_code, final_url)`.
- `FetchTimeout` (with or without partial HTML).
- Target-page HTTP errors (returned via `status_code`, not raised).
- Any `FetchError` subclass via `register_error`.
- Configurable `elapsed_ms` (defaults to a fixed 5 ms for determinism).

This is the primary tool for unit-testing the polling/parsing layers in later
phases.

## Recorded-transcript testing

For the real `PassePartoutClient`, we use **`pytest-recording`** (built on
`vcrpy`) to record HTTP interactions against a live passe-partout the first
time a test is run, then replay them deterministically thereafter.

- Cassettes live under `tests/fixtures/scraper/cassettes/<test_name>.yaml`.
- HTML fixtures (when stored separately for readability or reuse) live under
  `tests/fixtures/scraper/*.html`.
- Cassettes scrub the `Authorization` header before being written to disk
  (configured via `vcr_config` filter).
- Regeneration: `just record-scraper` (or `pytest --record-mode=rewrite -k
  scraper`) against a locally running `docker run … passe-partout`.
- Recorded scenarios at minimum:
  - `tests/scraper/test_client_success.py` — fetch a stable static page.
  - `tests/scraper/test_client_404.py` — fetch a known-404 URL, assert the
    status comes through in `FetchResult` rather than raising.
  - `tests/scraper/test_client_timeout.py` — point at a synthetic slow page;
    the cassette captures the partial state at 30 s.
  - `tests/scraper/test_client_auth.py` — bad token → `PassePartoutAuthError`.
  - `tests/scraper/test_client_busy.py` — synthesized 429 cassette →
    `PassePartoutBusy`.

If `pytest-recording` proves awkward (e.g. timing-sensitive cassettes), we fall
back to hand-rolled `respx` mocks for the same scenarios. Both approaches are
HTTP-level, not method-level, so they exercise the real `httpx` path.

## Module layout

```
veilleur/scraper/
├── __init__.py     # public API: Scraper, FetchResult, exceptions, factories
├── client.py       # PassePartoutClient (the real httpx-backed implementation)
├── fake.py         # FakePassePartout for tests of downstream code
└── errors.py       # FetchError hierarchy
```

### `__init__.py`
- Re-exports the `Scraper` Protocol, `FetchResult`, all exception classes,
  `PassePartoutClient`, and `FakePassePartout`.
- Provides `def client_from_env() -> PassePartoutClient` that reads
  `PASSEPARTOUT_URL` and `PASSEPARTOUT_BEARER_TOKEN` and constructs a client.
  Raises `RuntimeError` (not `FetchError`) if `PASSEPARTOUT_URL` is unset, since
  that is a config bug, not a runtime fetch failure.

### `client.py`
- `FetchResult` dataclass.
- `PassePartoutClient` class:
  - `__init__(base_url, bearer_token: str | None, *, timeout: float = 30.0,
    http_client: httpx.AsyncClient | None = None)`
  - Owns `asyncio.Lock` and `httpx.AsyncClient`.
  - `async fetch(url)` — wraps `_fetch_locked` in lock + timeout.
  - `_fetch_locked(url)` — the four-step flow, with `try/finally` cleanup.
  - `aclose()`, `__aenter__`, `__aexit__`.

### `fake.py`
- `FakePassePartout` with `register`, `register_timeout`, `register_error`.
- Tracks call history (`fake.calls: list[str]`) for assertion in tests.
- Honors the `Scraper` Protocol exactly so type-checking matches.

### `errors.py`
- `FetchError`, `FetchTimeout` (with `partial: FetchResult | None`),
  `PassePartoutUnavailable`, `PassePartoutAuthError`, `PassePartoutBusy`,
  `PassePartoutProtocolError`.

## Configuration

| Env var                     | Required | Notes                                              |
| --------------------------- | -------- | -------------------------------------------------- |
| `PASSEPARTOUT_URL`          | yes      | e.g. `http://127.0.0.1:8000`. TOML override allowed but env wins. |
| `PASSEPARTOUT_BEARER_TOKEN` | no       | When unset, no `Authorization` header is sent.     |

## Open questions / deferred items

- **Cookies (v2)** — Persist cookies per source so paywalled / login-required
  sites can be re-fetched without re-authenticating. Will use
  `GET /tabs/{id}/cookies` and the `cookies` field on `POST /tabs`.
- **Screenshots & console logs (v2)** — Useful for debugging xpath drift; would
  use `GET /tabs/{id}/screenshot` and require a passe-partout-side console
  capture endpoint we'd need to add.
- **Retries with backoff** — Deferred. The poll-cycle retry is sufficient until
  we have data showing transient failures dominate.
- **Multi-instance coordination** — Two Veilleurs against one passe-partout is
  unsupported. If we ever need it, options include: a server-side queue in
  passe-partout, a shared Redis lock, or per-Veilleur passe-partout instances.
- **Per-source timeout overrides** — Some sites legitimately take > 30 s. Would
  surface as a per-feed config field, plumbed into `fetch(url, timeout=)`.
- **Content-type handling** — v1 assumes `text/html`. PDFs / JSON / images
  arrive as downloads in passe-partout; we ignore them for now and let the
  caller see whatever HTML (or empty string) `/html` returns.
- **Health check on startup** — Should `client_from_env()` ping `/healthz`
  eagerly? Currently no: first failure surfaces on first fetch.
