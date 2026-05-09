"""Tests for ``veilleur.xpath.derive``."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.xpath.conftest import FakeLLMClient
from veilleur.xpath import (
    Anchor,
    HttpxLLMClient,
    LLMClientError,
    XPathDerivationFailed,
    derive_xpath,
    render_prompt,
)

SAMPLE_ANCHORS = [
    Anchor(xpath="/html/body/a[1]", text="One", href="/posts/one"),
    Anchor(xpath="/html/body/a[2]", text="Two", href="/posts/two"),
]


# --- Prompt rendering --------------------------------------------------------


def test_render_prompt_substitutes_title_url_listing() -> None:
    prompt = render_prompt("My Page", "https://example.com/", SAMPLE_ANCHORS)
    assert "My Page" in prompt
    assert "https://example.com/" in prompt
    assert "/html/body/a[1] | text='One' | href=/posts/one" in prompt
    assert "/html/body/a[2] | text='Two' | href=/posts/two" in prompt


def test_render_prompt_uses_repr_for_text() -> None:
    # text='...' uses Python repr which quotes the string. Match derive_xpath.py.
    prompt = render_prompt(
        "T",
        "u",
        [Anchor(xpath="/a", text="hello world", href="/x")],
    )
    assert "text='hello world'" in prompt


# --- derive_xpath ------------------------------------------------------------


async def test_returns_plain_xpath_unchanged() -> None:
    client = FakeLLMClient("//a[@class='post']")
    result = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert result == "//a[@class='post']"
    assert len(client.prompts) == 1


async def test_strips_fences() -> None:
    client = FakeLLMClient("```\n//a[@class='post']\n```")
    result = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert result == "//a[@class='post']"


async def test_strips_fences_with_xpath_tag() -> None:
    client = FakeLLMClient("```xpath\n//a[@class='post']\n```")
    result = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert result == "//a[@class='post']"


async def test_strips_thinking_block() -> None:
    client = FakeLLMClient(
        "<think>let me reason about this\nmulti-line</think>\n//a[@class='post']"
    )
    result = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert result == "//a[@class='post']"


async def test_strips_thinking_block_preserves_no_extra_whitespace() -> None:
    client = FakeLLMClient(
        "<think>reasoning here</think>\n\n   //a[@class='post']  \n\n"
    )
    result = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert result == "//a[@class='post']"


async def test_strips_thinking_block_with_fences() -> None:
    client = FakeLLMClient(
        "<thinking>step by step</thinking>\n```xpath\n//a[@class='post']\n```"
    )
    result = await derive_xpath("T", "u", SAMPLE_ANCHORS, client)
    assert result == "//a[@class='post']"


async def test_unable_lowercase_raises() -> None:
    client = FakeLLMClient("unable")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_unable_uppercase_raises() -> None:
    client = FakeLLMClient("Unable")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_unable_with_whitespace_raises() -> None:
    client = FakeLLMClient("  unable\n")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_empty_reply_raises() -> None:
    client = FakeLLMClient("")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_whitespace_only_reply_raises() -> None:
    client = FakeLLMClient("   \n  ")
    with pytest.raises(XPathDerivationFailed):
        await derive_xpath("T", "u", SAMPLE_ANCHORS, client)


async def test_prompt_includes_canonical_template_text() -> None:
    client = FakeLLMClient("//a")
    await derive_xpath("Title X", "https://x.example/", SAMPLE_ANCHORS, client)
    prompt = client.prompts[0]
    assert prompt.startswith("Given the following descriptive paths")
    assert "Title X" in prompt
    assert "https://x.example/" in prompt
    assert "single line" in prompt


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
    with httpx.Client() as _:
        pass
    # Use AsyncClient without entering context — only used for object identity.
    http = httpx.AsyncClient()
    try:
        with pytest.raises(LLMClientError):
            HttpxLLMClient(api_url="", model="m", api_key="k", http=http)
        with pytest.raises(LLMClientError):
            HttpxLLMClient(api_url="u", model="", api_key="k", http=http)
        with pytest.raises(LLMClientError):
            HttpxLLMClient(api_url="u", model="m", api_key="", http=http)
    finally:
        # AsyncClient.__del__ warns if not closed; close synchronously.
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
