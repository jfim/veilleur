"""XPath toolkit: anchor extraction, LLM-driven derivation, and application.

Three pure modules that, together, take an HTML page and produce a list
of feed items:

- :func:`extract_anchors` — parse HTML, return title + filtered anchors.
- :func:`derive_xpath` — ask an LLM for an xpath expression that selects
  article links from those anchors.
- :func:`apply_xpath` — evaluate the xpath against the HTML, returning
  resolved :class:`Item`\\s.

The orchestrator (Phase 4) wires these together and decides retry policy.
"""

from veilleur.xpath.anchors import extract_anchors
from veilleur.xpath.apply import apply_xpath
from veilleur.xpath.derive import (
    PROMPT_TEMPLATE,
    HttpxLLMClient,
    derive_xpath,
    render_prompt,
)
from veilleur.xpath.types import (
    Anchor,
    AnchorExtractionError,
    AnchorExtractionResult,
    Item,
    LLMClient,
    LLMClientError,
    XPathDerivationFailed,
    XPathNoMatchError,
    XPathSyntaxError,
    XPathToolkitError,
    XPathWrongElementError,
)

__all__ = [
    # Types
    "Anchor",
    "AnchorExtractionResult",
    "Item",
    "LLMClient",
    # Functions
    "extract_anchors",
    "derive_xpath",
    "apply_xpath",
    "render_prompt",
    "PROMPT_TEMPLATE",
    # Clients
    "HttpxLLMClient",
    # Exceptions
    "XPathToolkitError",
    "AnchorExtractionError",
    "XPathDerivationFailed",
    "LLMClientError",
    "XPathSyntaxError",
    "XPathNoMatchError",
    "XPathWrongElementError",
]
