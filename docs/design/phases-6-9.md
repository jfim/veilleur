# Phases 6–9 — REST API, Feed Serving, Scheduler, Web UI

Brief design hand-offs for the remaining implementation phases. Each section
is self-contained — a fresh agent can read just one phase's section plus
`README.md` and `docs/design/RESOLUTIONS.md` and produce a complete PR.

Phases 1–5 are already merged on `master`. The pipeline (`veilleur/pipeline/`)
exists and is the central thing every later phase composes with.

---

## Phase 6 — REST API

Build a FastAPI surface for managing feeds and reading items. Adds the JSON
API layer on top of the existing pipeline.

### Endpoints

- `POST /feeds` — create a feed `{url, title?, poll_interval_seconds?}` →
  returns the feed row
- `GET /feeds` — list feeds (paginate via `?limit=` / `?cursor=`)
- `GET /feeds/{id}` — single feed with active xpath, last status, last
  failure reason
- `PATCH /feeds/{id}` — change `title` / `poll_interval_seconds` / `status`
  (`active` / `paused`)
- `DELETE /feeds/{id}` — cascade-deletes items + runs
- `POST /feeds/{id}/scrape` — manually trigger a scrape; returns the
  `scrape_run` id (queues if a scrape is already running, since `run_scrape`
  is globally locked)
- `GET /feeds/{id}/items?limit=&cursor=&since=` — paginated history,
  newest first
- `GET /feeds/{id}/runs?limit=` — recent scrape runs with status

### Auth

`VEILLEUR_API_BEARER_TOKEN` (already declared in `veilleur/config.py`).
`Authorization: Bearer <token>` required for everything except `/healthz`.
When the env var is unset, all programmatic endpoints return 401.

### Notes

- Pydantic schemas in `veilleur/api/schemas.py`, routers in
  `veilleur/api/routes/` (one file per resource).
- Inject `Scraper` + `LLMClient` via FastAPI `Depends` so tests can swap
  them. Default factories build the real `PassePartoutClient` /
  `HttpxLLMClient` from settings.
- Reads `LLM_API_*` env vars for the real `HttpxLLMClient`. The current
  `HttpxLLMClient.from_env(http)` reads `os.environ` directly — consider
  switching to `get_settings()` for consistency, or add a thin wrapper
  that does. Keep both call sites green.
- Tests: httpx `AsyncClient` against the FastAPI app + testcontainers
  Postgres + `FakePassePartout` + a fake LLM client.

### Open question for that session

UUIDs in URLs vs. a separate human-readable slug. **Default to UUIDs** —
slugs add a uniqueness concern that doesn't pay off until there's an
end-user-facing URL surface (which there isn't until Phase 9, and even
there only the operator sees them).

---

## Phase 7 — RSS / Atom serving

Two read endpoints that render the feed's items as a syndication feed.

### Endpoints (no auth — these are public feed URLs)

- `GET /feeds/{id}/rss` — RSS 2.0
- `GET /feeds/{id}/atom` — Atom 1.0

### Notes

- Use `feedgen` (already in deps). Map: `Feed.title` → channel title,
  `Feed.url` → channel link, items → entries with `FeedItem.title`,
  `FeedItem.url`, `FeedItem.first_seen_at` as pubDate.
- Cap items per feed at e.g. 50 (most recent).
- `Cache-Control: public, max-age=300` is fine; clients re-fetch frequently.
- Set proper `Content-Type` (`application/rss+xml`, `application/atom+xml`).
- Tests: hit the endpoint, parse the XML with `lxml`, check it round-trips
  through a feed reader's expectations (entry count, links present, valid
  XML).

### Bundling

Trivial phase — can be bundled with Phase 6 if convenient (same router
structure).

---

## Phase 8 — Scheduler

Background loop that calls `run_scrape` on a schedule. Now that
`run_scrape` exists and is globally locked, this is just: "loop forever,
pick the next feed due, scrape it."

### Behavior

- Single asyncio task started on app startup (FastAPI lifespan handler).
- Every N seconds (configurable, default 30), query:
  ```sql
  SELECT id FROM feeds
  WHERE status = 'active'
    AND (last_scraped_at IS NULL
         OR last_scraped_at + poll_interval_seconds * INTERVAL '1 second' < now())
  ORDER BY last_scraped_at NULLS FIRST
  LIMIT 1
  ```
- If a feed is due, call `run_scrape(feed_id)`. If not, sleep.
- Graceful shutdown: cancel the task, wait for the current scrape to
  finish. The `asyncio.Lock` inside `run_scrape` lets it complete
  naturally.
- On uncaught exceptions inside `run_scrape`: log and continue. Never let
  the loop die.

### Notes

- New module `veilleur/scheduler/loop.py`.
- Config additions: `SCHEDULER_ENABLED` (default `true`),
  `SCHEDULER_TICK_SECONDS` (default 30).
- Tests: drive the loop manually with a fake clock and assert it picks
  the right feed, respects `paused`, and recovers from `run_scrape`
  raising.

### Edge case to flag

If `run_scrape` raises (vs. returning a `ScrapeOutcome` with
`status='failed'`), the scheduler must still continue. The pipeline is
written to convert most failures to outcomes, but defense-in-depth here
matters because this is the long-running loop.

---

## Phase 9 — Web UI

Server-rendered minimal UI per the project preference (no SPA). The user
has noted explicitly: "this is just an intermediate system, users
shouldn't really use it that much" — so prioritize functional over
polished.

### Pages

- `GET /` — list feeds (table: title, status, last_scraped_at,
  last_failure_reason, item count, manual-scrape button)
- `GET /feeds/{id}` — feed detail: recent items, recent runs, current
  xpath, edit form
- `POST /feeds` (form) — add feed
- `POST /feeds/{id}/scrape` (form) — trigger manual scrape
- `POST /feeds/{id}/pause` / `unpause` (form)
- `DELETE /feeds/{id}` (form via `_method` field)

### Notes

- **Jinja2** templates under `veilleur/web/templates/`. Static CSS under
  `veilleur/web/static/` — pick one of: vanilla CSS, Pico.css, simple
  Tailwind via CDN. Keep it boring.
- Tests: httpx against the templates, assert key elements present.

### Open question for that session

Auth strategy for the web UI. **Default recommendation: HTTP Basic with
the bearer token as the password.** Matches Phase 6's auth model and
works in any browser without JS. Alternatives: cookie-based session with
the bearer token as the password on a `/login` form, or trust the
network (LAN-only) and skip auth — flag the call to the user.

---

## Suggested order

`6 → 7 → 8 → 9`.

- Phase 7 is small and could ride along with 6 (both touch the same
  router structure).
- Phase 8 needs Phase 6 — the manual trigger endpoint is useful for
  verifying the scheduler in isolation before exposing it to real time.
- Phase 9 is last; it depends on Phase 6 routes and Phase 7 feed URLs to
  be useful.

## Cloud-session prompt template

> "Implement Phase _N_ of the Veilleur project per `docs/design/phases-6-9.md`
> in this repo. Read `README.md`, `docs/design/RESOLUTIONS.md`, and the
> existing `veilleur/pipeline/`, `veilleur/db/models/`, and
> `veilleur/scraper/` modules before designing. Run `just check` clean
> before opening a PR. PR base is `master`."
