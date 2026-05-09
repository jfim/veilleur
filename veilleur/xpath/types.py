"""Shared types for the xpath subpackage.

Hosts both:

- The xpath toolkit's data carriers and exception taxonomy (used by
  :mod:`~veilleur.xpath.anchors`, :mod:`~veilleur.xpath.derive`,
  :mod:`~veilleur.xpath.apply`).
- The validation result types used by :mod:`~veilleur.xpath.validation`.

Kept as one module so that future xpath-package modules can import either
group without circular-import gymnastics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

# --- Validation result types -------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedURL:
    """A URL after normalization, ready for byte-for-byte comparison.

    Query string and fragment are dropped. Scheme and host are lowercased.
    Default ports (80, 443) are stripped. Path segments have empty entries
    (from leading/trailing/repeated slashes) removed.
    """

    scheme: str
    host: str
    port: int | None
    segments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Ok:
    """All ``new_links`` matched the previous batch's LCP."""

    prefix: tuple[str, ...]
    matched: tuple[str, ...]
    kind: Literal["ok"] = "ok"


@dataclass(frozen=True, slots=True)
class Regenerate:
    """At least one ``new_link`` failed the previous batch's LCP.

    Strict 100% threshold: any unmatched link triggers ``Regenerate``.
    """

    prefix: tuple[str, ...]
    matched: tuple[str, ...]
    unmatched: tuple[str, ...]
    reason: str
    kind: Literal["regenerate"] = "regenerate"


@dataclass(frozen=True, slots=True)
class Fail:
    """A precondition was violated and we cannot meaningfully validate."""

    reason: str
    prefix: tuple[str, ...] | None
    unmatched: tuple[str, ...]
    kind: Literal["fail"] = "fail"


@dataclass(frozen=True, slots=True)
class FirstRun:
    """``prev_links`` was empty: there's nothing to compare against."""

    accepted: tuple[str, ...]
    kind: Literal["first_run"] = "first_run"


ValidationResult = Ok | Regenerate | Fail | FirstRun


# --- XPath toolkit types -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Anchor:
    """A single ``<a>`` element extracted from a page.

    Attributes:
        xpath: Absolute XPath of the element on the source tree.
        text: Whitespace-collapsed text content, truncated to 120 chars
            with an ellipsis if longer.
        href: Raw ``href`` attribute value, NOT yet absolutized.
    """

    xpath: str
    text: str
    href: str


@dataclass(frozen=True, slots=True)
class Item:
    """A feed item produced by applying an xpath to a page.

    Attributes:
        url: Absolute URL with any fragment stripped.
        title: Anchor text (falls back to last URL path segment if empty).
        source_xpath: The xpath expression that produced this item.
    """

    url: str
    title: str
    source_xpath: str


@dataclass(frozen=True, slots=True)
class AnchorExtractionResult:
    """Result of :func:`veilleur.xpath.anchors.extract_anchors`.

    Attributes:
        title: Page title (``"(untitled)"`` if missing).
        anchors: Anchors after filtering, in document order, capped at
            ``VEILLEUR_XPATH_MAX_ANCHORS`` if necessary.
        truncated: ``True`` if anchors were truncated by the cap.
        total_before_cap: Count of anchors after filtering, before the
            cap was applied (always ``>= len(anchors)``).
    """

    title: str
    anchors: list[Anchor]
    truncated: bool
    total_before_cap: int


class LLMClient(Protocol):
    """Minimal interface for an OpenAI-compatible chat completion call."""

    async def complete(self, prompt: str) -> str:
        """Send a single user-turn prompt; return the assistant's raw text reply."""
        ...


# --- Exceptions --------------------------------------------------------------


class XPathToolkitError(Exception):
    """Base class for all xpath-toolkit errors."""


class AnchorExtractionError(XPathToolkitError):
    """Raised when HTML cannot be parsed or contains no usable anchors."""


class XPathDerivationFailed(XPathToolkitError):
    """Raised when the LLM returns ``unable`` or an empty reply."""


class LLMClientError(XPathToolkitError):
    """Raised on transport, auth, or configuration failures of an `LLMClient`."""


class XPathSyntaxError(XPathToolkitError):
    """Raised when lxml fails to compile or evaluate the xpath expression."""


class XPathNoMatchError(XPathToolkitError):
    """Raised when the xpath expression matches zero elements."""


class XPathWrongElementError(XPathToolkitError):
    """Raised when the xpath matches at least one non-``<a>`` element."""
