"""Tests for ``veilleur.xpath.derive``."""

from __future__ import annotations

import json
from typing import Any

import httpx
import lxml.html
import pytest

from tests.xpath.conftest import FakeLLMClient
from veilleur.xpath import (
    Anchor,
    HttpxLLMClient,
    LLMClientError,
    XPathDerivationFailed,
    derive_xpath,
    extract_anchors,
    render_prompt,
)

SAMPLE_ANCHORS = [
    Anchor(xpath="/html/body/a[1]", text="One", href="/posts/one"),
    Anchor(xpath="/html/body/a[2]", text="Two", href="/posts/two"),
]

SAMPLE_HTML = """
<html><head><title>T</title></head>
<body>
  <a class="nav" href="/">home</a>
  <a class="post" href="/posts/one">One</a>
  <a class="post" href="/posts/two">Two</a>
  <a class="post" href="/posts/three">Three</a>
  <a class="footer" href="/about">about</a>
</body></html>
"""


# --- Prompt rendering --------------------------------------------------------


def test_render_prompt_substitutes_title_url_listing() -> None:
    prompt = render_prompt("My Page", "https://example.com/", SAMPLE_ANCHORS)
    assert "My Page" in prompt
    assert "https://example.com/" in prompt
    assert "id=1 | /html/body/a[1] | text='One' | href=/posts/one" in prompt
    assert "id=2 | /html/body/a[2] | text='Two' | href=/posts/two" in prompt


def test_render_prompt_uses_repr_for_text() -> None:
    prompt = render_prompt(
        "T",
        "u",
        [Anchor(xpath="/a", text="hello world", href="/x")],
    )
    assert "text='hello world'" in prompt


def test_render_prompt_includes_output_format_instructions() -> None:
    prompt = render_prompt("T", "u", SAMPLE_ANCHORS)
    assert "articles:" in prompt
    assert "xpath:" in prompt
    assert "unable" in prompt


def test_render_prompt_previous_attempts_empty_on_first_turn() -> None:
    prompt = render_prompt("T", "u", SAMPLE_ANCHORS, previous_attempts=[])
    assert "Previous attempts" not in prompt


# --- derive_xpath single-shot mode (no root) ---------------------------------


async def test_single_shot_returns_xpath() -> None:
    client = FakeLLMClient("articles: 1,2\nxpath: //a[@class='post']")
    out = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert out.xpath == "//a[@class='post']"
    assert len(out.attempts) == 1
    assert out.attempts[0].ok is True
    assert len(client.prompts) == 1


async def test_single_shot_strips_thinking_block() -> None:
    client = FakeLLMClient(
        "<think>reasoning</think>\narticles: 1,2\nxpath: //a[@class='post']"
    )
    out = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert out.xpath == "//a[@class='post']"


async def test_unable_raises_on_first_turn() -> None:
    client = FakeLLMClient("unable")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_unable_uppercase_raises() -> None:
    client = FakeLLMClient("Unable")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_empty_reply_exhausts_budget() -> None:
    client = FakeLLMClient(["", "", ""])
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client, max_attempts=3)


async def test_max_attempts_one_with_unparseable_reply() -> None:
    client = FakeLLMClient("just gibberish")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client, max_attempts=1)


# --- derive_xpath full loop with root ----------------------------------------


async def test_loop_succeeds_when_intended_matches_actual() -> None:
    extraction = extract_anchors(SAMPLE_HTML, "https://example.com/")
    # Three post anchors → ids 2, 3, 4 (after the nav anchor at id=1).
    # Wait: the listing is built from `extraction.anchors`. Let's find ids.
    post_ids = [
        i for i, a in enumerate(extraction.anchors, start=1) if a.href.startswith("/posts/")
    ]
    reply = (
        f"articles: {','.join(str(i) for i in post_ids)}\n"
        "xpath: //a[@class='post']"
    )
    client = FakeLLMClient(reply)
    out = await derive_xpath(
        extraction.title,
        "https://example.com/",
        extraction.anchors,
        client,
        root=extraction.root,
        elements=extraction.elements,
    )
    assert out.xpath == "//a[@class='post']"
    assert out.attempts[-1].ok is True
    assert sorted(out.attempts[-1].actual_ids) == sorted(post_ids)


async def test_loop_retries_on_mismatch_then_succeeds() -> None:
    extraction = extract_anchors(SAMPLE_HTML, "https://example.com/")
    post_ids = [
        i for i, a in enumerate(extraction.anchors, start=1) if a.href.startswith("/posts/")
    ]
    # First attempt: claims 'articles: post_ids' but xpath matches all <a>
    # (extras outside listing? all are in the listing) — actually //a matches
    # the nav and footer too, so actual will include id=nav and id=footer.
    bad_reply = (
        f"articles: {','.join(str(i) for i in post_ids)}\n"
        "xpath: //a"
    )
    good_reply = (
        f"articles: {','.join(str(i) for i in post_ids)}\n"
        "xpath: //a[@class='post']"
    )
    client = FakeLLMClient([bad_reply, good_reply])
    out = await derive_xpath(
        extraction.title,
        "https://example.com/",
        extraction.anchors,
        client,
        root=extraction.root,
        elements=extraction.elements,
        max_attempts=3,
    )
    assert len(out.attempts) == 2
    assert out.attempts[0].ok is False
    assert out.attempts[1].ok is True
    # Second prompt should mention prior attempt.
    assert "Previous attempts" in client.prompts[1]
    assert "//a" in client.prompts[1]


async def test_loop_exhausts_budget_when_never_matches() -> None:
    extraction = extract_anchors(SAMPLE_HTML, "https://example.com/")
    bad = "articles: 1\nxpath: //a"
    client = FakeLLMClient([bad, bad, bad])
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath(
            extraction.title,
            "https://example.com/",
            extraction.anchors,
            client,
            root=extraction.root,
            elements=extraction.elements,
            max_attempts=3,
        )


async def test_loop_invalid_xpath_counts_as_failed_attempt() -> None:
    extraction = extract_anchors(SAMPLE_HTML, "https://example.com/")
    bad_xpath = "articles: 1\nxpath: ///not-an-xpath["
    good_reply = "articles: 2,3,4\nxpath: //a[@class='post']"
    # ids for posts in this fixture happen to be 2,3,4 after nav.
    client = FakeLLMClient([bad_xpath, good_reply])
    out = await derive_xpath(
        extraction.title,
        "https://example.com/",
        extraction.anchors,
        client,
        root=extraction.root,
        elements=extraction.elements,
        max_attempts=2,
    )
    assert out.attempts[0].parse_error is not None
    assert out.attempts[1].ok is True


async def test_loop_unable_mid_loop_raises() -> None:
    extraction = extract_anchors(SAMPLE_HTML, "https://example.com/")
    client = FakeLLMClient(["articles: 1\nxpath: //a", "unable"])
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath(
            extraction.title,
            "https://example.com/",
            extraction.anchors,
            client,
            root=extraction.root,
            elements=extraction.elements,
            max_attempts=3,
        )


async def test_extras_outside_listing_blocks_ok() -> None:
    # Build a tree where xpath matches an <a> we filtered from anchors.
    html = """
    <html><body>
      <a href="javascript:void(0)" class="post">filtered</a>
      <a href="/posts/one" class="post">One</a>
    </body></html>
    """
    extraction = extract_anchors(html, "https://example.com/")
    # Only one anchor passes the filter (id=1).
    assert len(extraction.anchors) == 1
    # Model claims it intends id=1, but `//a[@class='post']` matches both
    # the filtered javascript: anchor and the kept one → extras=1, fail.
    bad = "articles: 1\nxpath: //a[@class='post']"
    good = "articles: 1\nxpath: //a[@href='/posts/one']"
    client = FakeLLMClient([bad, good])
    out = await derive_xpath(
        extraction.title,
        "https://example.com/",
        extraction.anchors,
        client,
        root=extraction.root,
        elements=extraction.elements,
        max_attempts=2,
    )
    assert out.attempts[0].extras_outside_listing == 1
    assert out.attempts[0].ok is False
    assert out.attempts[1].ok is True


# --- Prompt content sanity ---------------------------------------------------


async def test_prompt_includes_canonical_template_text() -> None:
    client = FakeLLMClient("articles: 1\nxpath: //a")
    await derive_xpath("Title X", "https://x.example/", SAMPLE_ANCHORS, client)
    prompt = client.prompts[0]
    assert prompt.startswith("Given the following links")
    assert "Title X" in prompt
    assert "https://x.example/" in prompt


# --- HttpxLLMClient ---------------------------------------------------------


def _mock_transport(handler) -> httpx.MockTransport:  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


async def test_httpx_client_sends_expected_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "//a[@class='ok']"}}]},
        )

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpxLLMClient(
            api_url="https://api.example.com/v1",
            model="gpt-test",
            api_key="secret",
            http=http,
        )
        out = await client.complete("hello")
    assert out == "//a[@class='ok']"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer secret"
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["temperature"] == 0.0
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]


async def test_httpx_client_strips_trailing_slash_on_api_url() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "//a"}}]})

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpxLLMClient(
            api_url="https://api.example.com/v1/",
            model="m",
            api_key="k",
            http=http,
        )
        await client.complete("p")
    assert captured["url"] == "https://api.example.com/v1/chat/completions"


async def test_httpx_client_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpxLLMClient(
            api_url="https://api.example.com/v1",
            model="m",
            api_key="k",
            http=http,
        )
        with pytest.raises(LLMClientError):
            await client.complete("p")


async def test_httpx_client_transport_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpxLLMClient(
            api_url="https://api.example.com/v1",
            model="m",
            api_key="k",
            http=http,
        )
        with pytest.raises(LLMClientError):
            await client.complete("p")


async def test_httpx_client_bad_response_shape_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    transport = _mock_transport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = HttpxLLMClient(
            api_url="https://api.example.com/v1",
            model="m",
            api_key="k",
            http=http,
        )
        with pytest.raises(LLMClientError):
            await client.complete("p")


def test_httpx_client_constructor_validates() -> None:
    http = httpx.AsyncClient()
    try:
        with pytest.raises(LLMClientError):
            HttpxLLMClient(api_url="", model="m", api_key="k", http=http)
        with pytest.raises(LLMClientError):
            HttpxLLMClient(api_url="u", model="", api_key="k", http=http)
        with pytest.raises(LLMClientError):
            HttpxLLMClient(api_url="u", model="m", api_key="", http=http)
    finally:
        import asyncio

        asyncio.get_event_loop().run_until_complete(http.aclose())


async def test_from_env_missing_vars_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    async with httpx.AsyncClient() as http:
        with pytest.raises(LLMClientError):
            HttpxLLMClient.from_env(http)


async def test_from_env_reads_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "key-123")
    async with httpx.AsyncClient() as http:
        client = HttpxLLMClient.from_env(http)
    assert client._model == "test-model"  # type: ignore[attr-defined]


# Silence pyright: lxml import is to avoid moving module-level fixture parsing.
_ = lxml.html
