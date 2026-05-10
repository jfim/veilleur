"""End-to-end tests for the Phase 6 REST API."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, FeedItem, ScrapeRun, XPathExtractor
from veilleur.scraper import FakePassePartout

from .conftest import TEST_BEARER_TOKEN, StubLLMClient

HTML_BASIC = """
<!doctype html>
<html><head><title>Blog</title></head>
<body>
<main>
  <article><h2><a href="/posts/2026/post-a">A</a></h2></article>
  <article><h2><a href="/posts/2026/post-b">B</a></h2></article>
</main>
</body></html>
"""

XPATH = "//main//article//h2/a"
# HTML_BASIC has 2 anchors → ids 1, 2; XPATH matches both.
XPATH_REPLY = f"articles: 1,2\nxpath: {XPATH}"


# --- Auth --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(api_client: httpx.AsyncClient) -> None:
    api_client.headers.pop("Authorization", None)
    r = await api_client.get("/feeds")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_bearer_token_rejected(api_client: httpx.AsyncClient) -> None:
    api_client.headers["Authorization"] = "Bearer wrong"
    r = await api_client.get("/feeds")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unset_token_rejects_all(api_app: FastAPI, api_client: httpx.AsyncClient) -> None:
    """When API_BEARER_TOKEN is unset, every API call is rejected."""
    from veilleur.config import get_settings

    old = os.environ.pop("API_BEARER_TOKEN", None)
    get_settings.cache_clear()
    try:
        r = await api_client.get("/feeds")
        assert r.status_code == 401
    finally:
        if old is not None:
            os.environ["API_BEARER_TOKEN"] = old
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_healthz_does_not_require_auth(api_client: httpx.AsyncClient) -> None:
    api_client.headers.pop("Authorization", None)
    r = await api_client.get("/healthz")
    assert r.status_code == 200


# --- CRUD --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_feed(api_client: httpx.AsyncClient) -> None:
    r = await api_client.post(
        "/feeds",
        json={"url": "https://example.com/", "title": "Example"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "Example"
    assert body["status"] == "active"
    assert body["poll_interval_seconds"] > 0
    assert body["active_xpath"] is None
    feed_id = body["id"]
    uuid.UUID(feed_id)  # validates shape

    r = await api_client.get(f"/feeds/{feed_id}")
    assert r.status_code == 200
    assert r.json()["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_create_feed_default_title(api_client: httpx.AsyncClient) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    assert r.status_code == 201
    assert r.json()["title"] == "https://example.com/"


@pytest.mark.asyncio
async def test_create_feed_rejects_duplicate_url(
    api_client: httpx.AsyncClient,
) -> None:
    await api_client.post("/feeds", json={"url": "https://example.com/"})
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_create_feed_validates_url(api_client: httpx.AsyncClient) -> None:
    r = await api_client.post("/feeds", json={"url": "not-a-url"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_list_feeds_paginates(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    for i in range(5):
        r = await api_client.post("/feeds", json={"url": f"https://example.com/{i}"})
        assert r.status_code == 201

    r = await api_client.get("/feeds", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    r2 = await api_client.get("/feeds", params={"limit": 2, "cursor": body["next_cursor"]})
    body2 = r2.json()
    assert len(body2["items"]) == 2
    seen = {it["id"] for it in body["items"]} | {it["id"] for it in body2["items"]}
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_list_feeds_invalid_cursor(api_client: httpx.AsyncClient) -> None:
    r = await api_client.get("/feeds", params={"cursor": "@@@not-base64"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_unknown_feed_returns_404(api_client: httpx.AsyncClient) -> None:
    r = await api_client.get(f"/feeds/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_feed(api_client: httpx.AsyncClient) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = r.json()["id"]

    r = await api_client.patch(
        f"/feeds/{feed_id}",
        json={"title": "Renamed", "poll_interval_seconds": 123, "status": "paused"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Renamed"
    assert body["poll_interval_seconds"] == 123
    assert body["status"] == "paused"


@pytest.mark.asyncio
async def test_patch_feed_invalid_status(api_client: httpx.AsyncClient) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = r.json()["id"]
    r = await api_client.patch(f"/feeds/{feed_id}", json={"status": "failed"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_resume_clears_failure_reason(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = uuid.UUID(r.json()["id"])
    async with api_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        feed.status = "failed"
        feed.last_failure_reason = "boom"
        await s.commit()

    r = await api_client.patch(f"/feeds/{feed_id}", json={"status": "active"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["last_failure_reason"] is None


@pytest.mark.asyncio
async def test_delete_feed_cascades(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = uuid.UUID(r.json()["id"])

    async with api_factory() as s:
        extractor = XPathExtractor(feed_id=feed_id, xpath=XPATH, generated_by="manual")
        s.add(extractor)
        await s.flush()
        s.add(
            FeedItem(
                feed_id=feed_id,
                guid="g1",
                url="https://example.com/post-a",
                title="A",
            )
        )
        s.add(ScrapeRun(feed_id=feed_id, status="success"))
        await s.commit()

    r = await api_client.delete(f"/feeds/{feed_id}")
    assert r.status_code == 204

    async with api_factory() as s:
        assert await s.get(Feed, feed_id) is None

    r = await api_client.delete(f"/feeds/{feed_id}")
    assert r.status_code == 404


# --- Items / runs ------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_endpoint_unknown_feed_404(
    api_client: httpx.AsyncClient,
) -> None:
    r = await api_client.get(f"/feeds/{uuid.uuid4()}/items")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_items_endpoint_paginates(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = uuid.UUID(r.json()["id"])
    async with api_factory() as s:
        for i in range(5):
            s.add(
                FeedItem(
                    feed_id=feed_id,
                    guid=f"g{i}",
                    url=f"https://example.com/p{i}",
                    title=f"P{i}",
                )
            )
        await s.commit()

    r = await api_client.get(f"/feeds/{feed_id}/items", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    r2 = await api_client.get(
        f"/feeds/{feed_id}/items",
        params={"limit": 2, "cursor": body["next_cursor"]},
    )
    body2 = r2.json()
    assert len(body2["items"]) == 2
    assert body["items"][0]["id"] != body2["items"][0]["id"]


@pytest.mark.asyncio
async def test_runs_endpoint(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = uuid.UUID(r.json()["id"])
    async with api_factory() as s:
        for status in ("success", "failed"):
            s.add(ScrapeRun(feed_id=feed_id, status=status))
        await s.commit()

    r = await api_client.get(f"/feeds/{feed_id}/runs")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2


# --- Scrape trigger ----------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_scrape_unknown_feed_404(
    api_client: httpx.AsyncClient,
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    r = await api_client.post(f"/feeds/{uuid.uuid4()}/scrape")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_trigger_scrape_first_run_derives_xpath(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = uuid.UUID(r.json()["id"])

    fake_scraper.register("https://example.com/", html=HTML_BASIC)
    stub_llm.queue(XPATH_REPLY)

    r = await api_client.post(f"/feeds/{feed_id}/scrape")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "xpath_regenerated"
    assert body["items_seen"] == 2
    assert body["items_new"] == 2
    assert body["error_message"] is None
    assert body["skipped"] is False
    uuid.UUID(body["scrape_run_id"])

    # GET after scrape: feed shows xpath, items endpoint returns rows.
    r = await api_client.get(f"/feeds/{feed_id}")
    assert r.json()["active_xpath"] == XPATH
    r = await api_client.get(f"/feeds/{feed_id}/items")
    items = r.json()["items"]
    assert len(items) == 2


@pytest.mark.asyncio
async def test_trigger_scrape_skipped_when_paused(
    api_client: httpx.AsyncClient,
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    r = await api_client.post("/feeds", json={"url": "https://example.com/"})
    feed_id = r.json()["id"]
    await api_client.patch(f"/feeds/{feed_id}", json={"status": "paused"})

    r = await api_client.post(f"/feeds/{feed_id}/scrape")
    assert r.status_code == 202
    body = r.json()
    assert body["skipped"] is True
    assert body["status"] == "skipped"
    assert fake_scraper.calls == []


# --- LLM dependency wrapper --------------------------------------------------


@pytest.mark.asyncio
async def test_get_llm_client_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DI factory pulls LLM creds from settings, not raw os.environ."""
    from veilleur.api import deps
    from veilleur.config import get_settings

    monkeypatch.setenv("LLM_API_URL", "http://example/v1")
    monkeypatch.setenv("LLM_MODEL_NAME", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "key-123")
    get_settings.cache_clear()
    try:
        client = await deps.get_llm_client()
        assert client is not None
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_get_llm_client_raises_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from veilleur.api import deps
    from veilleur.config import get_settings
    from veilleur.xpath import LLMClientError

    monkeypatch.delenv("LLM_API_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(LLMClientError):
            await deps.get_llm_client()
    finally:
        get_settings.cache_clear()


# Make linters happy: TEST_BEARER_TOKEN is intentionally re-exported for
# documentation; touch it once so the import isn't flagged as unused.
assert isinstance(TEST_BEARER_TOKEN, str)
