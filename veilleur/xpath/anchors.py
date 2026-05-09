"""Anchor extraction.

Parses an HTML page and returns the page title plus a filtered, ordered
list of ``<a>`` elements. Pure / synchronous / no I/O.

Logic ported verbatim from ``~/projects/rss-ify/dump_anchors.py`` and
``derive_xpath.py``: the absolute xpath is taken from
``getroottree().getpath(a)``; text is whitespace-collapsed and
truncated at 120 characters with an ellipsis; ``href`` is preserved
raw.
"""

from __future__ import annotations

import os

import lxml.etree
import lxml.html

from veilleur.xpath.types import (
    Anchor,
    AnchorExtractionError,
    AnchorExtractionResult,
)

#: Default cap on the number of anchors returned before LLM derivation.
#: Overridden by the ``VEILLEUR_XPATH_MAX_ANCHORS`` environment variable
#: at call time. Per the Phase 3 resolutions.
DEFAULT_MAX_ANCHORS: int = 250


def _max_anchors() -> int:
    raw = os.environ.get("VEILLEUR_XPATH_MAX_ANCHORS")
    if raw is None or raw == "":
        return DEFAULT_MAX_ANCHORS
    try:
        value = int(raw)
    except ValueError as exc:
        raise AnchorExtractionError(
            f"VEILLEUR_XPATH_MAX_ANCHORS must be an integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise AnchorExtractionError(f"VEILLEUR_XPATH_MAX_ANCHORS must be positive, got {value}")
    return value


def _is_useless_href(href: str) -> bool:
    """Return True if the anchor's href should be filtered out."""
    if not href or not href.strip():
        return True
    lowered = href.strip().lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:")):
        return True
    # Pure fragment: '#' alone, or '#something' (no path before it).
    return lowered.startswith("#")


def _normalize_text(raw: str) -> str:
    """Collapse whitespace and truncate at 120 chars with ellipsis."""
    text = " ".join(raw.split())
    if len(text) > 120:
        text = text[:117] + "..."
    return text


def extract_anchors(html: str, base_url: str) -> AnchorExtractionResult:
    """Parse ``html`` and extract usable anchors.

    Args:
        html: The page source as a string.
        base_url: The URL the page was fetched from. Validated as
            non-empty; not used by this function but kept on the
            signature for symmetry with the README contract.

    Returns:
        :class:`AnchorExtractionResult` containing the title, a filtered
        and possibly truncated list of anchors in document order, and a
        truncation flag/diagnostic.

    Raises:
        AnchorExtractionError: If the HTML is unparseable or no usable
            anchors remain after filtering.
    """
    if not base_url or not base_url.strip():
        raise AnchorExtractionError("base_url must be a non-empty string")

    try:
        root = lxml.html.fromstring(html)
    except (lxml.etree.ParserError, lxml.etree.XMLSyntaxError, ValueError) as exc:
        raise AnchorExtractionError(f"failed to parse HTML: {exc}") from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise AnchorExtractionError(f"failed to parse HTML: {exc}") from exc

    if root is None:
        raise AnchorExtractionError("failed to parse HTML: empty document")

    # Page title — match derive_xpath.py's logic.
    title_el = root.find(".//title")
    if title_el is not None and title_el.text and title_el.text.strip():
        title = title_el.text.strip()
    else:
        title = "(untitled)"

    rt = root.getroottree()
    anchors: list[Anchor] = []
    for a in root.xpath("//a"):
        href = a.get("href", "")
        if _is_useless_href(href):
            continue
        text = _normalize_text(a.text_content())
        anchors.append(Anchor(xpath=rt.getpath(a), text=text, href=href))

    if not anchors:
        raise AnchorExtractionError("no usable anchors")

    cap = _max_anchors()
    total_before_cap = len(anchors)
    if total_before_cap > cap:
        anchors = anchors[:cap]
        truncated = True
    else:
        truncated = False

    return AnchorExtractionResult(
        title=title,
        anchors=anchors,
        truncated=truncated,
        total_before_cap=total_before_cap,
    )
