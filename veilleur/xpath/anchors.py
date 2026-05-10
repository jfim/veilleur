"""Anchor extraction.

Parses an HTML page and returns the page title plus a filtered, ordered
list of ``<a>`` elements. Pure / synchronous / no I/O.

Each anchor's ``xpath`` field is a *descriptive* path — not a canonical
``getpath()``-style absolute xpath, but a CSS-ish breadcrumb that
includes ``id`` / ``class`` hints when present, falling back to a
positional ``[N]`` index among same-tag siblings only when neither is
available. The goal is to give the LLM enough structural context to
write robust xpath predicates instead of relying on fragile positions.
"""

from __future__ import annotations

import os
from typing import cast

import lxml.etree
import lxml.html

from veilleur.xpath.types import (
    Anchor,
    AnchorExtractionError,
    AnchorExtractionResult,
)

#: Default cap on the number of anchors returned before LLM derivation.
#: Overridden by the ``XPATH_MAX_ANCHORS`` environment variable
#: at call time. Per the Phase 3 resolutions.
DEFAULT_MAX_ANCHORS: int = 250


def _max_anchors() -> int:
    raw = os.environ.get("XPATH_MAX_ANCHORS")
    if raw is None or raw == "":
        return DEFAULT_MAX_ANCHORS
    try:
        value = int(raw)
    except ValueError as exc:
        raise AnchorExtractionError(
            f"XPATH_MAX_ANCHORS must be an integer, got {raw!r}"
        ) from exc
    if value <= 0:
        raise AnchorExtractionError(f"XPATH_MAX_ANCHORS must be positive, got {value}")
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


#: Cap on classes per segment to keep paths readable when an element
#: carries a long utility-class list (e.g. Tailwind).
_MAX_CLASSES_PER_SEGMENT = 3


def _segment_for(element: lxml.html.HtmlElement) -> str:
    """Build a single descriptive segment for *element*.

    Rules (in priority order):

    1. ``tag#id.class1.class2`` if both ``id`` and ``class`` present.
    2. ``tag#id`` if only ``id``.
    3. ``tag.class1.class2`` if only ``class`` (cap at three classes).
    4. ``tag[N]`` (1-indexed among same-tag siblings) otherwise.
    """
    tag = element.tag
    if not isinstance(tag, str):
        # Comments / processing instructions have non-string tags; skip.
        return "node"

    el_id = (element.get("id") or "").strip()
    classes = (element.get("class") or "").split()
    classes = [c for c in classes if c]

    if el_id and classes:
        kept = ".".join(classes[:_MAX_CLASSES_PER_SEGMENT])
        return f"{tag}#{el_id}.{kept}"
    if el_id:
        return f"{tag}#{el_id}"
    if classes:
        kept = ".".join(classes[:_MAX_CLASSES_PER_SEGMENT])
        return f"{tag}.{kept}"

    parent = element.getparent()
    if parent is None:
        return tag
    same_tag_siblings = [c for c in parent if c.tag == tag]
    if len(same_tag_siblings) <= 1:
        return tag
    index = same_tag_siblings.index(element) + 1
    return f"{tag}[{index}]"


def _build_descriptive_path(element: lxml.html.HtmlElement) -> str:
    """Walk from root to *element*, joining ``_segment_for`` with ``/``.

    The leading ``/`` mirrors absolute-xpath aesthetics so the model
    can mentally map "where on the page is this".
    """
    chain: list[lxml.html.HtmlElement] = []
    current: lxml.html.HtmlElement | None = element
    while current is not None:
        chain.append(current)
        current = cast("lxml.html.HtmlElement | None", current.getparent())
    chain.reverse()
    return "/" + "/".join(_segment_for(el) for el in chain)


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

    anchors: list[Anchor] = []
    elements: list[lxml.html.HtmlElement] = []
    for a in root.xpath("//a"):
        href = a.get("href", "")
        if _is_useless_href(href):
            continue
        text = _normalize_text(a.text_content())
        anchors.append(Anchor(xpath=_build_descriptive_path(a), text=text, href=href))
        elements.append(a)

    if not anchors:
        raise AnchorExtractionError("no usable anchors")

    cap = _max_anchors()
    total_before_cap = len(anchors)
    if total_before_cap > cap:
        anchors = anchors[:cap]
        elements = elements[:cap]
        truncated = True
    else:
        truncated = False

    return AnchorExtractionResult(
        title=title,
        anchors=anchors,
        truncated=truncated,
        total_before_cap=total_before_cap,
        root=root,
        elements=tuple(elements),
    )
