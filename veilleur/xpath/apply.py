"""XPath application and validation.

Compiles an xpath expression against a page, validates that the
matches are non-empty and all ``<a>`` elements, then turns each into
an :class:`Item` with absolutized, fragment-stripped URL and a sane
title.

This module is sync — it never calls the LLM.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import lxml.etree
import lxml.html

from veilleur.xpath.types import (
    AnchorExtractionError,
    Item,
    XPathNoMatchError,
    XPathSyntaxError,
    XPathWrongElementError,
)


def _last_path_segment(url: str) -> str:
    """Return the last non-empty path segment of *url*, else the URL itself."""
    parsed = urlparse(url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        return segments[-1]
    return url


def apply_xpath(html: str, base_url: str, xpath: str) -> list[Item]:
    """Run *xpath* against *html* and return resolved :class:`Item`\\s.

    Args:
        html: The page source as a string.
        base_url: The URL the page was fetched from. Used to absolutize
            relative ``href`` values via :func:`urllib.parse.urljoin`.
        xpath: An xpath expression. Should select ``<a>`` elements.

    Returns:
        Items in document order, deduped by absolute URL (first
        occurrence wins).

    Raises:
        XPathSyntaxError: The xpath fails to compile or evaluate.
        XPathNoMatchError: The xpath matches zero elements (or all
            matches dedupe to zero items).
        XPathWrongElementError: At least one match is not an ``<a>``.
        AnchorExtractionError: The HTML itself cannot be parsed.
    """
    if not base_url or not base_url.strip():
        raise AnchorExtractionError("base_url must be a non-empty string")

    try:
        root = lxml.html.fromstring(html)
    except (lxml.etree.ParserError, lxml.etree.XMLSyntaxError, ValueError) as exc:
        raise AnchorExtractionError(f"failed to parse HTML: {exc}") from exc

    try:
        matches: Any = root.xpath(xpath)
    except (lxml.etree.XPathSyntaxError, lxml.etree.XPathEvalError) as exc:
        raise XPathSyntaxError(f"invalid xpath {xpath!r}: {exc}") from exc

    if not isinstance(matches, list) or len(matches) == 0:
        raise XPathNoMatchError(f"xpath {xpath!r} matched no elements")

    # All matches must be <a> elements.
    for el in matches:
        tag = getattr(el, "tag", None)
        if tag != "a":
            raise XPathWrongElementError(f"xpath {xpath!r} matched non-<a> element {tag!r}")

    seen: set[str] = set()
    items: list[Item] = []
    for el in matches:
        href = el.get("href", "") or ""
        absolute = urljoin(base_url, href)
        absolute, _ = urldefrag(absolute)
        if not absolute:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        title = " ".join(el.text_content().split())
        if not title:
            title = _last_path_segment(absolute)
        items.append(Item(url=absolute, title=title, source_xpath=xpath))

    if not items:
        raise XPathNoMatchError(f"xpath {xpath!r} matched only items that deduped to empty")

    return items
