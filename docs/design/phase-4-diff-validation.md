# Phase 4 — Diff / Validation Logic

Module: `veilleur/xpath/validation.py`
Status: design (not yet implemented)

## 1. Goals

- Provide a **pure, total function** that compares the link list from the previous scrape against the link list from the current scrape and returns one of `Ok | Regenerate | Fail | FirstRun`.
- Match URLs against the **longest common prefix (LCP)** of the previous batch, with **numeric path segments treated as wildcards**, as described in `README.md` lines 13–18.
- Be deterministic, side-effect free, and trivially unit-testable. No HTTP, no DB, no clock, no logging.
- Return rich diagnostics (the prefix used, which links matched, which did not, why) so that callers (the scheduler / xpath-regenerator) can act and so that operators can debug.

## 2. Non-goals

- No I/O of any kind (no DB writes, no fetches, no file reads).
- No URL canonicalization beyond what's specified in §4 (we do not follow redirects, do not resolve relative URLs — inputs are assumed absolute).
- No xpath regeneration logic — that's a separate module; we only return the *signal* that triggers it.
- No content-based comparison (titles, hashes). Only URLs.
- No bot/SEO heuristics (no `www.` stripping, no trailing-slash equivalence beyond what `urlparse` gives us).

## 3. Result type

A **sealed tagged union** using `dataclasses` + a `Literal` discriminator. We pick this over plain `Enum` because each variant carries different diagnostics, and over inheritance because it pattern-matches cleanly with `match`/`case`.

```python
# veilleur/xpath/types.py
from dataclasses import dataclass
from typing import Literal, Union

@dataclass(frozen=True)
class Ok:
    kind: Literal["ok"] = "ok"
    prefix: tuple[str, ...]            # the LCP path segments used (no host)
    matched: tuple[str, ...]           # all new_links (all matched, by definition)

@dataclass(frozen=True)
class Regenerate:
    kind: Literal["regenerate"] = "regenerate"
    prefix: tuple[str, ...]            # the LCP that was tried
    matched: tuple[str, ...]           # subset of new_links that did match
    unmatched: tuple[str, ...]         # subset of new_links that did NOT match
    reason: str                        # human-readable, e.g. "3/10 links failed prefix"

@dataclass(frozen=True)
class Fail:
    kind: Literal["fail"] = "fail"
    reason: str                        # e.g. "empty new_links", "mixed hosts in prev"
    prefix: tuple[str, ...] | None     # None if we couldn't compute one
    unmatched: tuple[str, ...]         # may be empty

@dataclass(frozen=True)
class FirstRun:
    kind: Literal["first_run"] = "first_run"
    accepted: tuple[str, ...]          # == new_links

ValidationResult = Union[Ok, Regenerate, Fail, FirstRun]
```

`prefix` is stored as a `tuple[str, ...]` of normalized path segments, *not* a string, so callers can render it however they want and so equality is unambiguous. Host is implicit (it is shared by construction).

## 4. URL normalization

```
def normalize(url: str) -> NormalizedURL:
    p = urlparse(url)
    scheme = p.scheme.lower()                           # 'http' / 'https'
    host   = p.hostname.lower()                         # lowercase, no port if default
    port   = None if p.port in (None, 80, 443) else p.port
    # collapse repeated slashes in path, drop empty segments at ends
    raw_path = re.sub(r'/+', '/', p.path or '/')
    segments = tuple(s for s in raw_path.split('/') if s != '')
    # query and fragment are dropped entirely
    return NormalizedURL(scheme, host, port, segments)
```

```python
@dataclass(frozen=True)
class NormalizedURL:
    scheme: str                       # 'http' | 'https'
    host: str                         # lowercase, no port if default
    port: int | None
    segments: tuple[str, ...]         # path segments, no leading/trailing empties
```

Notes:
- Query string and fragment are **dropped**. Two URLs that differ only in `?utm_source=...` or `#section` normalize identically.
- Trailing slash is irrelevant (it produces no segment).
- Percent-encoded segments are **not** decoded — we compare them byte-for-byte after lowercasing scheme/host. This is conservative: `/posts/é` and `/posts/%C3%A9` would not match. Listed as an open question.
- We do **not** strip `www.`. Cross-host comparison is exact host match.

## 5. Numeric segment predicate

A path segment is "numeric" iff it contains **at least one ASCII digit**:

```python
NUMERIC_RE = re.compile(r'\d')
def is_numeric_segment(seg: str) -> bool:
    return bool(NUMERIC_RE.search(seg))
```

This matches: `2026`, `2026-05`, `page-3`, `post-12345`, `v1.2`, `chapter-1-intro`.
This does NOT match: `posts`, `articles`, `news`, `mypost`, `another`.

## 6. LCP algorithm (path-segment-wise, with numeric wildcards)

```
def lcp(prev_segments_list: list[tuple[str,...]]) -> tuple[str,...]:
    if not prev_segments_list:
        return ()
    if len(prev_segments_list) == 1:
        # Single-link / all-identical case: prefix is the directory
        # (drop the last segment — it's the "filename").
        only = prev_segments_list[0]
        return only[:-1] if only else ()

    prefix: list[str] = []
    for i, segs_at_i in enumerate(zip(*prev_segments_list)):
        # All URLs have a segment at position i.
        if all(is_numeric_segment(s) for s in segs_at_i):
            # Numeric wildcard position — keep going, store sentinel.
            prefix.append("*")
            continue
        if all(s == segs_at_i[0] for s in segs_at_i):
            prefix.append(segs_at_i[0])
            continue
        # Mismatch on a non-numeric segment — stop.
        break
    return tuple(prefix)
```

The sentinel `"*"` distinguishes a wildcard slot from a literal segment. We store wildcards positionally so that `shares_prefix` can require *some* segment at that position rather than ignoring it.

The "all-zip" form requires that every prev URL has at least `len(prefix)` segments; URLs shorter than the LCP-so-far naturally cap the loop because `zip` stops at the shortest input.

### Minimum prefix rule

After computing LCP, require **≥1 non-numeric (non-`"*"`) segment**. If that condition fails, the prefix is too weak to validate against → return `Fail("prefix too weak: no non-numeric segments", prefix=lcp)`.

## 7. `shares_prefix` predicate

```
def shares_prefix(url_segments: tuple[str,...], prefix: tuple[str,...]) -> bool:
    if len(url_segments) < len(prefix):
        return False
    for u, p in zip(url_segments, prefix):
        if p == "*":
            # wildcard: must exist (it does — len check above) AND must itself be numeric
            if not is_numeric_segment(u):
                return False
        else:
            if u != p:
                return False
    return True
```

Wildcards must be **filled with a numeric segment** in the candidate URL. `/posts/2026/x.html` matches prefix `('posts','*')`; `/posts/about/x.html` does not.

## 8. Decision tree (exhaustive)

Inputs: `prev_links: list[str]`, `new_links: list[str]`.

```
1. If prev_links is empty (or None):
     -> FirstRun(accepted=tuple(new_links))           # even if new_links is also empty

2. (prev_links non-empty from here on.)
   If new_links is empty:
     -> Fail(reason="empty new_links with non-empty prev",
             prefix=None, unmatched=())

3. Normalize all prev_links and new_links.
   Collect prev hosts := { (scheme-insensitive,) host : prev_links }.
   If len(distinct hosts in prev) > 1:
     -> Fail(reason="mixed hosts in prev (precondition violation)",
             prefix=None, unmatched=())
   Let H := the single prev host.

4. Filter new_links to those whose normalized host == H.
     cross_host := new_links with host != H
     same_host  := new_links with host == H

5. Compute prefix := lcp([n.segments for n in normalized_prev]).
   If prefix has zero non-numeric segments:
     -> Fail(reason="prefix too weak (no non-numeric segments)",
             prefix=prefix, unmatched=tuple(new_links))

6. For each new link, evaluate shares_prefix(seg, prefix).
     matched   := new_links where host==H AND shares_prefix
     unmatched := new_links where host!=H OR not shares_prefix

7. Decide:
     if not matched and not unmatched:
         # impossible here (new_links non-empty), but defensively:
         -> Fail(reason="no links to evaluate", prefix=prefix, unmatched=())
     if not unmatched:                  # 100% matched
         -> Ok(prefix=prefix, matched=tuple(matched))
     if not matched:                    # 0% matched
         -> Regenerate(prefix=prefix, matched=(), unmatched=tuple(unmatched),
                       reason="no new links matched previous prefix")
     # mixed: strict 100% threshold
     -> Regenerate(prefix=prefix,
                   matched=tuple(matched),
                   unmatched=tuple(unmatched),
                   reason=f"{len(unmatched)}/{len(new_links)} links failed prefix")
```

Note: per the consolidated decisions, **strict 100%** is the Ok threshold. Even a single failure → `Regenerate`. The caller is then free to retry with a regenerated xpath; if that retry's result is also `Regenerate` or `Fail`, the caller (not us) escalates to "feed broken".

## 9. Worked examples

### 9.1 README example — happy path

```
prev = [https://example.com/posts/2026/mypost.html,
        https://example.com/posts/2026/another.html]
new  = [https://example.com/posts/2026/third.html,
        https://example.com/posts/2026/fourth.html]

normalized prev segments: [('posts','2026','mypost.html'),
                           ('posts','2026','another.html')]
LCP per position:
  pos 0: 'posts' == 'posts'          -> 'posts'
  pos 1: '2026','2026' both numeric  -> '*'
  pos 2: 'mypost.html' != 'another'  -> stop
prefix = ('posts', '*')   (1 non-numeric segment — passes minimum)
new links: each has segments ('posts','2026',_) — both shares_prefix.
=> Ok(prefix=('posts','*'), matched=(...two...))
```

### 9.2 README example — schema drift

```
prev = same as 9.1
new  = [https://example.com/something.html]
new[0].segments = ('something.html',) — len < len(prefix)=2
=> shares_prefix False
=> Regenerate(prefix=('posts','*'), matched=(), unmatched=(...one...),
              reason="no new links matched previous prefix")
```

### 9.3 Single prev link

```
prev = [https://example.com/blog/2026/post-1.html]
LCP single-link branch → prefix = directory = ('blog','2026')
But '2026' is numeric → only non-numeric segment is 'blog' → passes minimum.
Wait: stored as literals (single-link branch returns the segments verbatim,
not as wildcards). So prefix = ('blog','2026') with no '*'.
new = [https://example.com/blog/2026/post-2.html]   -> Ok
new = [https://example.com/blog/2027/post-2.html]   -> Regenerate
       (because '2027' != literal '2026'; single-link branch does not
        introduce wildcards, by design — there is no second sample to
        infer a wildcard from)
```

This is an intentional asymmetry: wildcards exist only where the data shows variation. Single-link prev is therefore stricter. Listed as an open question (§13).

### 9.4 All prev identical

Same as single-link: dedupe yields one URL, take its directory. (Implementation may dedupe explicitly or fall through `lcp` since identical inputs collapse naturally.)

### 9.5 Mixed hosts in prev

```
prev = [https://a.com/x/1.html, https://b.com/x/2.html]
=> Fail(reason="mixed hosts in prev (precondition violation)", prefix=None)
```

### 9.6 Empty new

```
prev = [https://example.com/posts/2026/x.html]
new  = []
=> Fail(reason="empty new_links with non-empty prev", prefix=None)
```

### 9.7 Query-string-only difference

```
prev = [https://ex.com/p/2026/a.html?utm=1]
new  = [https://ex.com/p/2026/a.html?utm=2]
After normalize, query is dropped → both have segments ('p','2026','a.html').
prefix (single-link) = ('p','2026') → match → Ok
```

### 9.8 Fragment-only difference

Same outcome as 9.7 — fragments are dropped during normalization.

### 9.9 Cross-host new

```
prev = [https://ex.com/p/2026/a.html, https://ex.com/p/2026/b.html]
new  = [https://cdn.ex.com/p/2026/c.html]
prev host = 'ex.com'; new host = 'cdn.ex.com' (no www-stripping, exact match).
=> all new fall to unmatched → Regenerate
```

### 9.10 First run

```
prev = []
new  = [https://ex.com/x.html]
=> FirstRun(accepted=('https://ex.com/x.html',))
```

## 10. Edge cases table

| # | prev | new | expected |
|---|---|---|---|
| 1 | `[]` | `[a]` | `FirstRun` |
| 2 | `[]` | `[]` | `FirstRun` (accepted=()) |
| 3 | `[a]` | `[]` | `Fail` (empty new) |
| 4 | `[hostA/x, hostB/y]` | `[hostA/z]` | `Fail` (mixed prev hosts) |
| 5 | `[ex.com/posts/2026/a, ex.com/posts/2026/b]` | `[ex.com/posts/2026/c]` | `Ok` |
| 6 | same | `[ex.com/posts/2027/c]` | `Ok` (wildcard at pos 1) |
| 7 | same | `[ex.com/posts/about]` | `Regenerate` (`about` not numeric) |
| 8 | same | `[ex.com/something.html]` | `Regenerate` (too short) |
| 9 | same | `[ex.com/posts/2026/c, ex.com/x.html]` | `Regenerate` (1/2 unmatched) |
| 10 | `[ex.com/posts/2026/a]` | `[ex.com/posts/2026/b]` | `Ok` (single-prev, dir match) |
| 11 | `[ex.com/posts/2026/a]` | `[ex.com/posts/2027/b]` | `Regenerate` (single-prev is literal) |
| 12 | `[ex.com/2026/a]` | `[ex.com/2026/b]` | `Fail` (prefix too weak: only `2026`, numeric) |
| 13 | `[HTTPS://EX.COM/p/1.html, https://ex.com/p/2.html]` | `[https://ex.com/p/3.html]` | `Ok` (scheme/host case-insensitive) |
| 14 | `[ex.com/p/a?x=1]` | `[ex.com/p/a?x=2]` | `Ok` (query dropped) |
| 15 | `[ex.com/p/a#h1]` | `[ex.com/p/a#h2]` | `Ok` (fragment dropped) |
| 16 | `[ex.com:443/p/a, ex.com/p/b]` | `[ex.com/p/c]` | `Ok` (default port stripped) |
| 17 | `[ex.com//p///a]` | `[ex.com/p/a]` | `Ok` (slash collapse) |
| 18 | `[ex.com/blog/2026, ex.com/blog/2027]` | `[ex.com/blog/2028]` | `Ok` (LCP=`('blog','*')`, new has `*` filled) |
| 19 | `[ex.com/blog/2026, ex.com/blog/2027]` | `[ex.com/blog/about]` | `Regenerate` (`about` not numeric, can't fill `*`) |
| 20 | `[ex.com/a/1, ex.com/b/2]` | `[ex.com/a/3]` | `Fail` (LCP=`()`, no non-numeric) |

## 11. Test strategy

### 11.1 Table-driven cases
The 20 rows above become parametrized `pytest` cases. Each row asserts the variant kind plus key payload fields (prefix, matched, unmatched lengths). Diagnostic strings are matched loosely (substring).

### 11.2 Property tests (`hypothesis`)

1. **Purity**: `validate(prev, new) == validate(prev, new)` for all inputs (no hidden state).
2. **Order independence of prev**: shuffling `prev_links` does not change the variant or `prefix` set. (Matched/unmatched ordering may follow `new_links` ordering — we'll preserve input order.)
3. **Order independence of new (for variant)**: shuffling `new_links` does not change the variant kind, nor the *set* of matched/unmatched.
4. **Idempotence under normalization**: appending random `?utm=...&fbclid=...` query strings or `#anchors` to any URL in prev or new does not change the result.
5. **Self-match**: `validate(prev, prev) ∈ {Ok}` whenever `prev` is non-empty, single-host, and its LCP has ≥1 non-numeric segment.
6. **Strict-100% monotonicity**: if `validate(prev, new) == Ok` and you append an unmatched link to `new`, the result becomes `Regenerate` (never `Ok`, never `Fail`).
7. **Empty new is always Fail (when prev non-empty)**, regardless of prev contents (assuming prev is single-host).
8. **Wildcard sanity**: any URL whose only difference from a prev URL is a digit-bearing segment at a wildcard position must be accepted (assuming non-numeric segments around it are equal).

## 12. Module layout

```
veilleur/xpath/
├── __init__.py
├── types.py          # NormalizedURL, Ok, Regenerate, Fail, FirstRun, ValidationResult
└── validation.py     # normalize(), is_numeric_segment(), lcp(), shares_prefix(), validate()
```

Rationale: types in `types.py` keeps them importable from elsewhere in `veilleur/xpath/` (e.g. an eventual regenerator module) without a circular dep on `validation.py`. Public entry point is `veilleur.xpath.validation.validate(prev_links, new_links) -> ValidationResult`.

Tests live in `tests/xpath/test_validation.py` (table-driven) and `tests/xpath/test_validation_properties.py` (hypothesis).

## 13. Open questions

1. **Percent-encoding equivalence.** Currently `/é` and `/%C3%A9` differ. Should we percent-decode segments before comparison? Probably yes, but defer to a follow-up.
2. **Single-prev wildcard inference.** Should a single prev URL like `/blog/2026/post-1` infer a wildcard at `2026` (because the segment is numeric), making prefix `('blog','*')`? Currently no — we treat single-prev as literal. Consider revisiting if false-positive `Regenerate`s are observed in production.
3. **Trailing-slash semantics on directories.** We treat `/posts` and `/posts/` identically (both yield `('posts',)`). Confirm this is desired for sites that distinguish them.
4. **IDN / Punycode hosts.** We lowercase ASCII bytes; IDN normalization (`idna.encode`) is not applied. Likely fine for scope, but flag.
5. **Mixed-host prev as recoverable.** Currently `Fail`. Could instead pick the majority host and proceed. Deferred — keep `Fail` until we see real data.
6. **Should `FirstRun` carry the inferred prefix** (computed from `new_links` itself) so the caller can persist it? Currently no — caller persists raw links and we recompute next run. Revisit if it becomes a perf concern.
