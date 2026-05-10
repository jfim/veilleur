"""HTTP-level tests for ``PassePartoutClient`` using respx mocks.

These exercise the real ``httpx`` path through the four-step tab flow,
covering the success case and each documented error path.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from veilleur.scraper import (
    FetchTimeout,
    PassePartoutAuthError,
    PassePartoutBusy,
    PassePartoutClient,
    PassePartoutUnavailable,
    UnsupportedContentType,
)

BASE = "http://passe-partout.test"


def _make_client(
    bearer_token: str | None = None,
    *,
    wait_timeout: float = 30.0,
    local_budget: float | None = None,
) -> PassePartoutClient:
    return PassePartoutClient(
        base_url=BASE,
        bearer_token=bearer_token,
        wait_timeout=wait_timeout,
        local_budget=local_budget,
    )


@respx.mock
async def test_client_success_full_flow() -> None:
    create = respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-1",
                "status": 200,
                "final_url": "https://example.com/landed",
                "content_type": "text/html; charset=utf-8",
            },
        )
    )
    wait = respx.post(f"{BASE}/tabs/tab-1/wait").mock(
        return_value=httpx.Response(200, json={"network_idle": True})
    )
    html = respx.get(f"{BASE}/tabs/tab-1/html").mock(
        return_value=httpx.Response(
            200, text="<html><body>hello</body></html>"
        )
    )
    delete = respx.delete(f"{BASE}/tabs/tab-1").mock(
        return_value=httpx.Response(204)
    )

    async with _make_client() as client:
        result = await client.fetch("https://example.com")

    assert result.html == "<html><body>hello</body></html>"
    assert result.status_code == 200
    assert result.final_url == "https://example.com/landed"
    assert result.elapsed_ms >= 0
    assert create.called
    assert wait.called
    assert html.called
    assert delete.called


@respx.mock
async def test_client_wait_408_proceeds_with_partial_html() -> None:
    """A 408 from /wait (networkidle didn't settle) is non-fatal — fetch HTML anyway."""
    respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-408",
                "status": 200,
                "final_url": "https://example.com/slow",
                "content_type": "text/html",
            },
        )
    )
    respx.post(f"{BASE}/tabs/tab-408/wait").mock(
        return_value=httpx.Response(
            408, json={"error": "timeout", "detail": "wait timed out"}
        )
    )
    html = respx.get(f"{BASE}/tabs/tab-408/html").mock(
        return_value=httpx.Response(200, text="<html>partial</html>")
    )
    respx.delete(f"{BASE}/tabs/tab-408").mock(return_value=httpx.Response(204))

    async with _make_client() as client:
        result = await client.fetch("https://example.com/slow")

    assert result.status_code == 200
    assert result.html == "<html>partial</html>"
    assert html.called


@respx.mock
async def test_client_target_page_404_does_not_raise() -> None:
    """A 404 from the *target site* is reported via ``status_code``, not raised."""
    respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-2",
                "status": 404,
                "final_url": "https://example.com/missing",
                "content_type": "text/html",
            },
        )
    )
    respx.post(f"{BASE}/tabs/tab-2/wait").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/tabs/tab-2/html").mock(
        return_value=httpx.Response(200, text="<h1>not found</h1>")
    )
    respx.delete(f"{BASE}/tabs/tab-2").mock(return_value=httpx.Response(204))

    async with _make_client() as client:
        result = await client.fetch("https://example.com/missing")

    assert result.status_code == 404
    assert "not found" in result.html


@respx.mock
async def test_client_auth_error_on_create() -> None:
    respx.post(f"{BASE}/tabs").mock(return_value=httpx.Response(401))

    async with _make_client(bearer_token="bad") as client:
        with pytest.raises(PassePartoutAuthError):
            await client.fetch("https://example.com")


@respx.mock
async def test_client_busy_on_create() -> None:
    respx.post(f"{BASE}/tabs").mock(return_value=httpx.Response(429))

    async with _make_client() as client:
        with pytest.raises(PassePartoutBusy):
            await client.fetch("https://example.com")


@respx.mock
async def test_client_5xx_unavailable() -> None:
    respx.post(f"{BASE}/tabs").mock(return_value=httpx.Response(503))

    async with _make_client() as client:
        with pytest.raises(PassePartoutUnavailable):
            await client.fetch("https://example.com")


@respx.mock
async def test_client_connect_error_unavailable() -> None:
    respx.post(f"{BASE}/tabs").mock(side_effect=httpx.ConnectError("refused"))

    async with _make_client() as client:
        with pytest.raises(PassePartoutUnavailable):
            await client.fetch("https://example.com")


@respx.mock
async def test_client_unsupported_content_type() -> None:
    respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-3",
                "status": 200,
                "final_url": "https://example.com/file.pdf",
                "content_type": "application/pdf",
            },
        )
    )
    # DELETE must still be called from the finally clause.
    delete = respx.delete(f"{BASE}/tabs/tab-3").mock(
        return_value=httpx.Response(204)
    )

    async with _make_client() as client:
        with pytest.raises(UnsupportedContentType) as exc:
            await client.fetch("https://example.com/file.pdf")

    assert exc.value.content_type == "application/pdf"
    assert delete.called


@respx.mock
async def test_client_xhtml_content_type_accepted() -> None:
    respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-x",
                "status": 200,
                "final_url": "https://example.com/x",
                "content_type": "application/xhtml+xml; charset=utf-8",
            },
        )
    )
    respx.post(f"{BASE}/tabs/tab-x/wait").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/tabs/tab-x/html").mock(
        return_value=httpx.Response(200, text="<html/>")
    )
    respx.delete(f"{BASE}/tabs/tab-x").mock(return_value=httpx.Response(204))

    async with _make_client() as client:
        result = await client.fetch("https://example.com/x")

    assert result.html == "<html/>"


@respx.mock
async def test_client_timeout_with_partial_html() -> None:
    """When the budget expires, a best-effort GET /html supplies a partial result."""

    async def slow_wait(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2.0)
        return httpx.Response(200, json={})

    respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-slow",
                "status": 200,
                "final_url": "https://slow.example",
                "content_type": "text/html",
            },
        )
    )
    respx.post(f"{BASE}/tabs/tab-slow/wait").mock(side_effect=slow_wait)
    # The best-effort partial GET after timeout returns this.
    respx.get(f"{BASE}/tabs/tab-slow/html").mock(
        return_value=httpx.Response(200, text="<partial/>")
    )
    respx.delete(f"{BASE}/tabs/tab-slow").mock(return_value=httpx.Response(204))

    async with _make_client(wait_timeout=0.05, local_budget=0.2) as client:
        with pytest.raises(FetchTimeout) as exc_info:
            await client.fetch("https://slow.example")

    assert exc_info.value.partial is not None
    assert exc_info.value.partial.html == "<partial/>"
    assert exc_info.value.partial.status_code == 200


@respx.mock
async def test_client_sends_authorization_header_when_token_set() -> None:
    captured: dict[str, str] = {}

    def _check(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        captured["auth"] = auth
        return httpx.Response(
            200,
            json={
                "id": "tab-a",
                "status": 200,
                "final_url": "https://example.com",
                "content_type": "text/html",
            },
        )

    respx.post(f"{BASE}/tabs").mock(side_effect=_check)
    respx.post(f"{BASE}/tabs/tab-a/wait").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/tabs/tab-a/html").mock(
        return_value=httpx.Response(200, text="ok")
    )
    respx.delete(f"{BASE}/tabs/tab-a").mock(return_value=httpx.Response(204))

    async with _make_client(bearer_token="s3cret") as client:
        await client.fetch("https://example.com")

    assert captured["auth"] == "Bearer s3cret"


@respx.mock
async def test_client_no_authorization_header_when_token_unset() -> None:
    captured: dict[str, str | None] = {}

    def _check(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "id": "tab-n",
                "status": 200,
                "final_url": "https://example.com",
                "content_type": "text/html",
            },
        )

    respx.post(f"{BASE}/tabs").mock(side_effect=_check)
    respx.post(f"{BASE}/tabs/tab-n/wait").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/tabs/tab-n/html").mock(
        return_value=httpx.Response(200, text="ok")
    )
    respx.delete(f"{BASE}/tabs/tab-n").mock(return_value=httpx.Response(204))

    async with _make_client(bearer_token=None) as client:
        await client.fetch("https://example.com")

    assert captured["auth"] is None


@respx.mock
async def test_client_singleflight_serializes_concurrent_fetches() -> None:
    """Two ``fetch`` calls on the same client must not overlap."""
    in_flight = 0
    max_in_flight = 0

    async def create(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(
            200,
            json={
                "id": "tab-sf",
                "status": 200,
                "final_url": "https://example.com",
                "content_type": "text/html",
            },
        )

    respx.post(f"{BASE}/tabs").mock(side_effect=create)
    respx.post(f"{BASE}/tabs/tab-sf/wait").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/tabs/tab-sf/html").mock(
        return_value=httpx.Response(200, text="ok")
    )
    respx.delete(f"{BASE}/tabs/tab-sf").mock(return_value=httpx.Response(204))

    async with _make_client() as client:
        await asyncio.gather(
            client.fetch("https://example.com/a"),
            client.fetch("https://example.com/b"),
            client.fetch("https://example.com/c"),
        )

    assert max_in_flight == 1


@respx.mock
async def test_client_delete_failure_is_swallowed() -> None:
    """If DELETE fails, fetch still returns the result successfully."""
    respx.post(f"{BASE}/tabs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "tab-d",
                "status": 200,
                "final_url": "https://example.com",
                "content_type": "text/html",
            },
        )
    )
    respx.post(f"{BASE}/tabs/tab-d/wait").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{BASE}/tabs/tab-d/html").mock(
        return_value=httpx.Response(200, text="ok")
    )
    respx.delete(f"{BASE}/tabs/tab-d").mock(return_value=httpx.Response(500))

    async with _make_client() as client:
        result = await client.fetch("https://example.com")

    assert result.html == "ok"


def test_client_from_env_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """``client_from_env`` raises ``RuntimeError`` (not ``FetchError``) on missing URL."""
    from veilleur.config import Settings
    from veilleur.scraper import client_from_env

    fake = Settings.model_construct(PASSEPARTOUT_URL=None, PASSEPARTOUT_BEARER_TOKEN=None)
    monkeypatch.setattr("veilleur.scraper.get_settings", lambda: fake)
    with pytest.raises(RuntimeError):
        client_from_env()
