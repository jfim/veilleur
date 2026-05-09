# Phase 3 — XPath Toolkit

Three pure modules under `veilleur/xpath/` that, together, take an HTML page and produce a list of feed items: **anchor extraction**, **LLM-driven xpath derivation**, and **xpath application**. Each module is independently testable; the orchestrator (Phase 4) composes them and handles persistence + failure marking.

Reference implementation lives at `~/projects/rss-ify/` (`dump_anchors.py`, `derive_xpath.py`, `prompt.txt`, `match_xpath.py`, `extract_xpath.py`). This phase ports its core logic into a typed, async, provider-agnostic library.

## Goals

- Pure, side-effect-free functions (parsing, prompt building, HTTP to LLM) that take inputs and return values.
- Provider-agnostic LLM access via OpenAI-compatible chat-completions HTTP endpoint, configured by env vars `LLM_API_URL`, `LLM_MODEL_NAME`, `LLM_API_KEY`.
- A typed error taxonomy so the orchestrator can distinguish transient from permanent failures and mark feeds appropriately.
- Verbatim reuse of the canonical prompt from `prompt.txt` and the text-normalization rules from `dump_anchors.py`.

## Non-goals

- No HTTP fetching of HTML (Phase 2 / `scraper` does that via passe-partout).
- No persistence (Phase 4).
- No retry / backoff policy on LLM calls (orchestrator decides).
- No prompt caching, no few-shot examples, no auto-repair of malformed XPath. v1 is single-shot, temperature 0.
- No HTML rendering / JS execution.

## Module layout

```
veilleur/xpath/
  __init__.py     # re-exports the public surface
  types.py        # dataclasses + exceptions
  anchors.py      # extract_anchors(html, base_url) -> (title, [Anchor])
  derive.py       # derive_xpath(title, url, anchors, client) -> str   (async)
  apply.py        # apply_xpath(html, base_url, xpath) -> [Item]
```

`__init__.py` re-exports: `Anchor`, `Item`, `LLMClient`, `extract_anchors`, `derive_xpath`, `apply_xpath`, and the exception classes.

## Dependency change: drop `anthropic`

The Phase 0 scaffolding pinned `anthropic` in `pyproject.toml`. Phase 3 makes the LLM choice an env-var concern, so the SDK pin is no longer warranted.

- **Remove** `anthropic` from `[project].dependencies`.
- **Keep** `httpx` (already present) — used directly for the OpenAI-compatible chat-completions call.
- **Justification for `httpx` over the `openai` SDK**: zero added surface, easy to mock in tests (just patch the `LLMClient` Protocol), and the request/response shape is small enough that an SDK adds no value. The same `LLM_API_URL` env var pointed at OpenAI, an Anthropic-compatible proxy, Ollama, vLLM, or LM Studio all work without code change.

## Public types

```python
# veilleur/xpath/types.py
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True, slots=True)
class Anchor:
    xpath: str   # absolute xpath of the <a> element on the source tree
    text: str    # whitespace-collapsed, truncated to 120 chars with ellipsis
    href: str    # raw href attribute value, NOT yet absolutized

@dataclass(frozen=True, slots=True)
class Item:
    url: str            # absolute, fragment-stripped
    title: str          # anchor text, or last URL segment if text is empty
    source_xpath: str   # the xpath that produced this item (for debugging)


class LLMClient(Protocol):
    async def complete(self, prompt: str) -> str:
        """Send a single user-turn prompt; return the assistant's raw text reply."""
        ...
```

### Exceptions

```python
class XPathToolkitError(Exception): ...
class AnchorExtractionError(XPathToolkitError): ...        # parser failed, no <a> found, etc.
class XPathDerivationFailed(XPathToolkitError): ...        # LLM returned 'unable' or empty
class LLMClientError(XPathToolkitError): ...               # transport/auth failure
class XPathSyntaxError(XPathToolkitError): ...             # lxml raised on compile/eval
class XPathNoMatchError(XPathToolkitError): ...            # xpath ran but matched zero nodes
class XPathWrongElementError(XPathToolkitError): ...       # matched non-<a> elements
```

`XPathDerivationFailed` is the umbrella the orchestrator catches to mark a feed as failed-to-derive. `XPathNoMatchError` and `XPathWrongElementError` are raised by `apply_xpath`; the orchestrator may catch them to trigger re-derivation per the rules in README.md.

## Module 1 — `anchors.py`

```python
def extract_anchors(html: str, base_url: str) -> tuple[str, list[Anchor]]: ...
```

### Behavior

1. Parse with `lxml.html.fromstring(html)`. On parse error, raise `AnchorExtractionError`.
2. Pull the page title from `//title` (strip; fallback `"(untitled)"`), matching `derive_xpath.py`.
3. Iterate `tree.xpath("//a")`. For each anchor build `Anchor(xpath, text, href)` where:
   - `xpath = root.getroottree().getpath(a)` — absolute xpath, ported verbatim from `dump_anchors.py`.
   - `text = " ".join(a.text_content().split())`; if `len(text) > 120`, truncate to 117 chars + `"..."`. Verbatim from rss-ify.
   - `href = a.get("href", "")`. Stored raw (not absolutized) — the LLM should see what the page actually shows.
4. **Filter** anchors before returning. An anchor is dropped if its `href` is:
   - empty / whitespace-only,
   - starts with `javascript:`, `mailto:`, `tel:` (case-insensitive),
   - a pure fragment (`href` is `#` or starts with `#` and has no path).
5. Preserve document order. If the resulting list is empty, raise `AnchorExtractionError("no usable anchors")`.
6. `base_url` is accepted (and validated as a URL) but only used downstream by `apply_xpath`. Storing it here keeps the function signature symmetric with the README's `(html, base_url) -> (title, [Anchor])`.

### Caps

No hard cap on anchors sent to the LLM in v1 — the reference page (`the-batch.html`) ran fine. Document this as an open question (see end). If we hit context limits in practice, the natural cap is "drop trailing footer anchors" or "stop at N=500".

## Module 2 — `derive.py`

```python
async def derive_xpath(
    title: str,
    url: str,
    anchors: list[Anchor],
    client: LLMClient,
) -> str: ...
```

### Behavior

1. Build the `listing` block by joining `f"{a.xpath} | text={a.text!r} | href={a.href}"` per anchor — exact format from `derive_xpath.py`.
2. Render the prompt template (canonical: `prompt.txt`, ported verbatim into `derive.py` as a module constant `PROMPT_TEMPLATE`).
3. Call `await client.complete(prompt)`.
4. Strip whitespace; strip leading/trailing triple-backtick fences and an optional `xpath` language tag — same logic as the reference's response cleanup.
5. If the cleaned reply equals `"unable"` (case-insensitive) or is empty, raise `XPathDerivationFailed`.
6. Return the raw xpath string. **No** validation here — that's `apply_xpath`'s job. Keeping this module pure-text means we can unit-test it with a `FakeLLMClient` and never touch lxml.

### The exact prompt (verbatim from `~/projects/rss-ify/prompt.txt`)

```
Given the following xpath absolute paths for links on a webpage, return an xpath expression that only matches links to articles. Do not return navigation links, tag/category links, or other irrelevant links, just links that appear to be an article so that the user can use them to build a RSS feed. The page is called {title} and is from {url}.

{listing}

Return only an xpath expression if you are able to match all of the articles and no other unwanted links, with no commentary or other messages, no markdown, no code fences, no backticks — just the raw xpath expression on a single line. If you are unable to write such an xpath expression, just write 'unable'.
```

(`prompt.txt` is canonical; the JSON-formatted prompt in `extract_xpath.py` is research scratch and is not used.)

### Response shape

The contract with the model is **plain text, single line**, NOT JSON. Either:

- a single xpath expression (which we treat as opaque text and pass straight to lxml), or
- the literal string `unable`.

Fenced-code-block tolerance is kept since real models add fences despite instructions. Sentinel match is case-insensitive.

## LLM client abstraction

The `LLMClient` Protocol (defined in `types.py`) keeps `derive.py` provider-agnostic. Two concrete implementations live alongside, in `derive.py`:

```python
class HttpxLLMClient:
    """OpenAI-compatible chat-completions client backed by httpx.AsyncClient."""
    def __init__(
        self,
        api_url: str,         # e.g. https://api.openai.com/v1  -- /chat/completions appended
        model: str,
        api_key: str,
        http: httpx.AsyncClient,
        temperature: float = 0.0,
    ): ...

    @classmethod
    def from_env(cls, http: httpx.AsyncClient) -> "HttpxLLMClient":
        # Reads LLM_API_URL, LLM_MODEL_NAME, LLM_API_KEY; raises LLMClientError if missing.
        ...

    async def complete(self, prompt: str) -> str: ...
```

Request body sent to `{api_url}/chat/completions`:

```json
{
  "model": "<LLM_MODEL_NAME>",
  "temperature": 0.0,
  "messages": [{"role": "user", "content": "<rendered prompt>"}]
}
```

with header `Authorization: Bearer <LLM_API_KEY>`. Response is parsed as `data["choices"][0]["message"]["content"]`. Any non-2xx response → `LLMClientError`. Network/timeout errors → `LLMClientError`.

Env-var flow: the orchestrator (or the FastAPI app on startup) constructs one `HttpxLLMClient.from_env(...)` and injects it into `derive_xpath`. The xpath subpackage never reads env vars itself, keeping it pure.

For tests, a `FakeLLMClient` lives in `tests/xpath/conftest.py` and just returns canned replies.

## Module 3 — `apply.py`

```python
def apply_xpath(html: str, base_url: str, xpath: str) -> list[Item]: ...
```

### Behavior

1. Parse `html` with `lxml.html.fromstring`. (We re-parse rather than threading a tree through, so each module is independent.)
2. Compile/evaluate `tree.xpath(xpath)`. Catch `lxml.etree.XPathSyntaxError` and `lxml.etree.XPathEvalError` → raise our `XPathSyntaxError` with the offending expression in the message.
3. If the result list is empty → `XPathNoMatchError`.
4. If any matched element is not an `<a>` (i.e. `el.tag != "a"`) → `XPathWrongElementError`. (The README's "non-anchor xpath result is an error" rule.)
5. For each matched `<a>`:
   - Resolve its `href` against `base_url` via `urllib.parse.urljoin`.
   - Strip the URL fragment (`urllib.parse.urldefrag`).
   - Compute title: `" ".join(a.text_content().split())`; if empty, fall back to the last non-empty path segment of the URL.
   - Construct `Item(url=..., title=..., source_xpath=xpath)`.
6. Dedupe by `url`, **preserving first-occurrence order**.
7. Return `[Item]`.

This module never calls the LLM and has no async surface — it's CPU-only and stays sync.

## XPath validation rules (summary)

| Rule | Where checked | Raises |
|---|---|---|
| Compiles as valid XPath 1.0 | `apply_xpath` (lxml) | `XPathSyntaxError` |
| Matches ≥ 1 element | `apply_xpath` | `XPathNoMatchError` |
| All matches are `<a>` elements | `apply_xpath` | `XPathWrongElementError` |
| Resulting items list non-empty after dedupe | `apply_xpath` | `XPathNoMatchError` (if dedupe collapses to 0) |

The Phase 4 orchestrator additionally validates "links match prior-run prefix" per README, but that is **not** in the xpath module.

## Failure-handling contract for the orchestrator

The xpath toolkit is pure: it raises typed exceptions and lets the caller decide. The expected orchestrator flow:

1. `extract_anchors` raises `AnchorExtractionError` → mark feed as failed (page is unparseable or has no anchors).
2. `derive_xpath` raises `XPathDerivationFailed` → mark feed as failed (LLM declined). `LLMClientError` → transient; retry per orchestrator policy.
3. `apply_xpath` raises `XPathSyntaxError` / `XPathWrongElementError` / `XPathNoMatchError` → orchestrator may try **one** re-derivation. If the second derivation also fails to apply, mark feed as failed.

The xpath modules themselves do not retry, do not regenerate, and do not log to a sink — that all belongs upstream.

## Test strategy

### Unit tests (`tests/xpath/`)

- `tests/xpath/fixtures/` — checked-in HTML samples:
  - `the-batch.html` (copied from rss-ify — known good baseline)
  - `simple-blog.html` — minimal hand-crafted, two posts in a `<ul>`
  - `mixed-nav.html` — articles plus nav, footer, social links (filter exercise)
  - `js-mailto.html` — anchors with `javascript:`, `mailto:`, `tel:`, pure-`#` (filter exercise)
  - `relative-urls.html` — relative hrefs to test `urljoin`
  - `fragments.html` — hrefs with `#anchor` to test fragment stripping
  - `dupes.html` — same href appearing twice (dedupe order test)
  - `malformed.html` — broken HTML lxml should still parse
  - `empty-text.html` — anchors with no text → URL-segment title fallback
- **`anchors.py` tests**: parse each fixture; assert title, anchor count, filter behavior, text truncation, document order.
- **`derive.py` tests**: with `FakeLLMClient`:
  - returns plain xpath → returned unchanged
  - returns fenced xpath → fences stripped
  - returns `unable` / `Unable` / `unable\n` → `XPathDerivationFailed`
  - returns empty string → `XPathDerivationFailed`
  - prompt rendering: capture the prompt the fake received, snapshot-test the title/url/listing substitution.
- **`apply.py` tests**: each fixture × valid xpath → expected item list; invalid xpath → `XPathSyntaxError`; xpath that matches `<div>` → `XPathWrongElementError`; xpath matching nothing → `XPathNoMatchError`; relative + fragment + dedupe assertions.

### Live tests

- A `@pytest.mark.live` marker, skipped by default.
- Live test path: real `HttpxLLMClient.from_env()` + the-batch fixture → expect a non-empty `[Item]` list and that all items' URLs start with `https://www.deeplearning.ai/the-batch/`. Loose-but-meaningful assertion that survives model drift.
- CI: live tests not run; `just test-live` invokes them locally with `LLM_*` env vars set.

### Mocked LLM client design

```python
class FakeLLMClient:
    def __init__(self, replies: list[str] | str):
        self._replies = [replies] if isinstance(replies, str) else list(replies)
        self.prompts: list[str] = []
    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._replies.pop(0)
```

Lives in `tests/xpath/conftest.py` as a fixture. Keeps tests fast and offline.

## Open questions / deferred

- **Anchor cap**: do we truncate at N=500 anchors before sending to LLM? Defer until we observe a real failure.
- **Multi-candidate xpath**: prompt could ask for top-3 candidates; today it asks for one. Defer.
- **Self-repair on `XPathSyntaxError`**: a follow-up turn telling the model "your xpath was invalid" could fix typos. Not in v1.
- **Prompt caching**: the prompt is large (every anchor on the page) and stable per feed across runs. Worth caching once we pick a provider that supports it. Out of scope for v1.
- **Title extraction**: today we use `<title>`. Some pages use `<meta property="og:title">` which is often cleaner. Defer.
- **Anchor filtering subtlety**: should we drop anchors whose `href` resolves *outside* `base_url`'s host? Probably not — many feeds (link aggregators) legitimately point off-site. Leave to the model.
- **Removing `anthropic` from pyproject.toml**: requires a small follow-up patch in Phase 3 implementation. Document in this design; execute alongside the module code.
