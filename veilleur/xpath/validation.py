"""Pure diff/validation logic for xpath link batches.

Given the previous batch of links and the current batch of links extracted
by an xpath expression, decide whether the xpath is still valid (``Ok``),
appears to be drifting (``Regenerate``), is in an unrecoverable state
(``Fail``), or whether this is the first scrape (``FirstRun``).

This module is **pure**: no I/O, no DB, no network, no clock, no logging.
The result types live in :mod:`veilleur.xpath.types`.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from veilleur.xpath.types import (
    Fail,
    FirstRun,
    NormalizedURL,
    Ok,
    Regenerate,
    ValidationResult,
)

__all__ = [
    "WILDCARD",
    "is_numeric_segment",
    "lcp",
    "normalize",
    "shares_prefix",
    "validate",
]

# Sentinel marker used in computed prefixes to indicate "this position must
# be filled with a numeric segment but its value may vary". Stored as a
# string so prefixes can be tuples of plain strings.
WILDCARD = "*"

_NUMERIC_RE = re.compile(r"\d")
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def is_numeric_segment(segment: str) -> bool:
    """A path segment is numeric iff it contains at least one ASCII digit."""

    return bool(_NUMERIC_RE.search(segment))


def normalize(url: str) -> NormalizedURL:
    """Normalize a URL for byte-for-byte comparison.

    - Lowercase scheme and host.
    - Strip the port if it's the scheme's default (80 for http, 443 for https).
    - Collapse repeated slashes in the path.
    - Split into non-empty path segments.
    - Drop query string and fragment entirely.
    """

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()

    raw_port = parsed.port
    if raw_port is None or raw_port == _DEFAULT_PORTS.get(scheme):
        port: int | None = None
    else:
        port = raw_port

    raw_path = re.sub(r"/+", "/", parsed.path or "/")
    segments = tuple(s for s in raw_path.split("/") if s != "")

    return NormalizedURL(scheme=scheme, host=host, port=port, segments=segments)


def lcp(prev_segments_list: list[tuple[str, ...]]) -> tuple[str, ...]:
    """Path-segment-wise LCP with numeric wildcards.

    Rules:
        - Empty input -> ``()``.
        - Single input (or all-identical inputs collapse to this case via
          ``zip``): return the "directory" segments — drop the final
          segment, treated as the filename. No wildcards are inferred:
          there is no second sample to demonstrate variation.
        - Multi-input: at each position, if all entries are numeric
          segments, store ``WILDCARD``; if all entries are equal,
          store the literal; otherwise stop. ``zip`` naturally caps the
          loop at the shortest input.
    """

    if not prev_segments_list:
        return ()
    if len(prev_segments_list) == 1:
        only = prev_segments_list[0]
        return only[:-1] if only else ()

    prefix: list[str] = []
    for segs_at_i in zip(*prev_segments_list, strict=False):
        if all(is_numeric_segment(s) for s in segs_at_i):
            prefix.append(WILDCARD)
            continue
        first = segs_at_i[0]
        if all(s == first for s in segs_at_i):
            prefix.append(first)
            continue
        break
    return tuple(prefix)


def shares_prefix(url_segments: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """Does ``url_segments`` start with ``prefix``?

    Wildcard positions in ``prefix`` require that ``url_segments`` has a
    *numeric* segment at that position; they're not "match anything"
    placeholders. Literal positions require exact equality. The candidate
    URL must have at least as many segments as the prefix.
    """

    if len(url_segments) < len(prefix):
        return False
    for u, p in zip(url_segments, prefix, strict=False):
        if p == WILDCARD:
            if not is_numeric_segment(u):
                return False
        elif u != p:
            return False
    return True


def validate(prev_links: list[str] | None, new_links: list[str]) -> ValidationResult:
    """Compare ``new_links`` against ``prev_links``.

    Returns one of :class:`Ok`, :class:`Regenerate`, :class:`Fail`, or
    :class:`FirstRun`. See ``docs/design/phase-4-diff-validation.md`` for
    the full decision tree.
    """

    # 1. First run.
    if not prev_links:
        return FirstRun(accepted=tuple(new_links))

    # 2. Empty new with non-empty prev -> Fail.
    if not new_links:
        return Fail(
            reason="empty new_links with non-empty prev",
            prefix=None,
            unmatched=(),
        )

    # 3. Normalize and check single-host precondition on prev.
    normalized_prev = [normalize(u) for u in prev_links]
    prev_hosts = {n.host for n in normalized_prev}
    if len(prev_hosts) > 1:
        return Fail(
            reason=f"mixed hosts in prev (precondition violation): {sorted(prev_hosts)!r}",
            prefix=None,
            unmatched=(),
        )

    expected_host = next(iter(prev_hosts))

    # 5. Compute prefix. Dedupe first so that "all-identical prev" collapses
    # to the single-link branch (directory of the only URL) rather than
    # the multi-link branch (which would return the full segments verbatim
    # because every position trivially agrees).
    #
    # No minimum-prefix rule — an empty prefix is legal for root-level
    # feeds per phase-4 resolution.
    unique_segments: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for n in normalized_prev:
        if n.segments not in seen:
            seen.add(n.segments)
            unique_segments.append(n.segments)
    prefix = lcp(unique_segments)

    # 6. Bucket new links into matched/unmatched, preserving input order.
    matched: list[str] = []
    unmatched: list[str] = []
    for original, n in zip(new_links, (normalize(u) for u in new_links), strict=True):
        if n.host != expected_host:
            unmatched.append(original)
            continue
        if shares_prefix(n.segments, prefix):
            matched.append(original)
        else:
            unmatched.append(original)

    # 7. Decide.
    if not unmatched:
        return Ok(prefix=prefix, matched=tuple(matched))
    if not matched:
        return Regenerate(
            prefix=prefix,
            matched=(),
            unmatched=tuple(unmatched),
            reason="no new links matched previous prefix",
        )
    return Regenerate(
        prefix=prefix,
        matched=tuple(matched),
        unmatched=tuple(unmatched),
        reason=f"{len(unmatched)}/{len(new_links)} links failed prefix",
    )
