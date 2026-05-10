# Design Review Resolutions

Resolutions to the open questions raised in `phase-1-foundations.md`,
`phase-2-passe-partout-client.md`, `phase-3-xpath-toolkit.md`, and
`phase-4-diff-validation.md`. Implementers must follow both the original
design doc and the resolutions below; where they conflict, the resolutions win.

## Phase 1 — Foundations

- **Raw HTML in `scrape_runs`**: keep it in Postgres for v1 (useful for
  debugging xpath regressions). Move to object storage later — tracked as a
  follow-up issue. Apply column-level compression where available.
- **Programmatic API auth**: Veilleur itself exposes a bearer-token-protected
  REST API. Add `API_BEARER_TOKEN` env var (optional; when unset, the
  API rejects all programmatic requests except `/healthz`). LLM_API_KEY and
  PASSEPARTOUT_BEARER_TOKEN are unrelated to this — those are outbound creds.
- **`feeds.last_failure_reason`**: add a nullable `VARCHAR` column on `feeds`
  storing a human-readable reason for the most recent failure, e.g.:
  - `"HTTP 502 from server"`
  - `"Server returned image/png instead of text/html"`
  - `"Unable to find XPath expression because LLM returned 'unable'"`
  Cleared on the next successful scrape. This complements (does not replace)
  `scrape_runs.error_message` — the runs row is per-attempt history; the feeds
  column is the latest-failure quick read.

## Phase 2 — Passe-partout client

- **Content-type enforcement**: any response whose `Content-Type` is not
  `text/html` (or `application/xhtml+xml`) fails the fetch. Raise
  `UnsupportedContentType("server returned <ctype> instead of text/html")`.
  The orchestrator catches this and writes the reason into
  `feeds.last_failure_reason`.

## Phase 3 — XPath toolkit

- **Anchor cap**: configurable. Add env var `XPATH_MAX_ANCHORS`
  (default `250`). The anchor extractor truncates to this count before
  sending to the LLM. Document the truncation in the result (e.g., a flag on
  the returned `(title, anchors)` tuple, or a separate diagnostic) so the
  caller can log it.

## Phase 4 — Diff/validation

- **Minimum prefix**: removed. A feed whose posts live at the host root
  (e.g., `example.com/post-1`, `example.com/post-2`) is legal. The prefix
  may legitimately be just the host (no path segments).
- **URL normalization before comparison**: confirmed yes (was already in the
  design — explicitly affirmed).
- **LCP for `/blog/2026/post-1`**: the LCP across two such URLs is `/blog/`,
  not `/blog/2026/post-` — i.e., once an LCP step encounters a numeric
  segment that doesn't match a non-numeric segment in the other URL (or two
  numeric segments with different values), the LCP terminates *before* the
  numeric segment. Numeric segments are wildcards only when both sides have
  a numeric segment in that position. Update the worked examples accordingly.
