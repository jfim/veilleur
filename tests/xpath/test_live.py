"""Live (network) tests for the xpath toolkit.

Skipped unless ``LLM_API_KEY`` is set. Invoke locally via ``just`` or
``pytest -m live``. These hit the configured LLM endpoint with the
``the-batch.html`` fixture and assert that the resulting items all
look like article URLs from deeplearning.ai.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.xpath.conftest import load_fixture
from veilleur.xpath import (
    HttpxLLMClient,
    apply_xpath,
    derive_xpath,
    extract_anchors,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("LLM_API_KEY"),
        reason="LLM_API_KEY not set; skipping live tests",
    ),
]


async def test_live_the_batch_end_to_end() -> None:
    base_url = "https://www.deeplearning.ai/the-batch/"
    html = load_fixture("the-batch.html")

    extraction = extract_anchors(html, base_url)
    assert extraction.anchors

    async with httpx.AsyncClient(timeout=120) as http:
        client = HttpxLLMClient.from_env(http)
        xpath = await derive_xpath(extraction.title, base_url, extraction.anchors, client)
    assert xpath
    items = apply_xpath(html, base_url, xpath)
    assert items
    for item in items:
        assert item.url.startswith("https://www.deeplearning.ai/the-batch/")
