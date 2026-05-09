"""Phase 9 web UI tests.

Drives the templated routes through ``httpx.AsyncClient`` against the
FastAPI app, sharing the existing ``api_factory`` / ``api_app`` fixtures
in ``tests/api/conftest.py`` so we get the same testcontainer-backed DB,
truncate-between-tests semantics, and bearer-token configuration.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, FeedItem, ScrapeRun
from veilleur.scraper import FakePassePartout

from ..api.conftest import TEST_BEARER_TOKEN, StubLLMClient

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


def _basic_auth_header(password: str = TEST_BEARER_TOKEN, user: str = "admin") -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


@pytest_asyncio.fixture
async def web_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": _basic_auth_header()},
        follow_redirects=False,
    ) as client:
        yield client


# --- Auth --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_requires_basic_auth(web_client: httpx.AsyncClient) -> None:
    web_client.headers.pop("Authorization", None)
    r = await web_client.get("/")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].lower().startswith("basic")


@pytest.mark.asyncio
async def test_wrong_basic_password_rejected(web_client: httpx.AsyncClient) -> None:
    web_client.headers["Authorization"] = _basic_auth_header(password="nope")
    r = await web_client.get("/")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unset_token_rejects_web_ui(
    api_app: FastAPI, web_client: httpx.AsyncClient
) -> None:
    from veilleur.config import get_settings

    old = os.environ.pop("VEILLEUR_API_BEARER_TOKEN", None)
    get_settings.cache_clear()
    try:
        r = await web_client.get("/")
        assert r.status_code == 401
    finally:
        if old is not None:
            os.environ["VEILLEUR_API_BEARER_TOKEN"] = old
        get_settings.cache_clear()


# --- Index -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_lists_feeds(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_factory() as s:
        s.add(Feed(url="https://example.com/a", title="Alpha"))
        s.add(Feed(url="https://example.com/b", title="Beta"))
        await s.commit()

    r = await web_client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Alpha" in body
    assert "Beta" in body
    assert "Add feed" in body


@pytest.mark.asyncio
async def test_index_empty_state(web_client: httpx.AsyncClient) -> None:
    r = await web_client.get("/")
    assert r.status_code == 200
    assert "No feeds yet" in r.text


@pytest.mark.asyncio
async def test_index_shows_item_count_and_failure(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_factory() as s:
        feed = Feed(
            url="https://example.com/",
            title="Example",
            status="failed",
            last_failure_reason="HTTP 502 from server",
        )
        s.add(feed)
        await s.flush()
        for i in range(3):
            s.add(
                FeedItem(
                    feed_id=feed.id,
                    guid=f"g{i}",
                    url=f"https://example.com/p{i}",
                    title=f"P{i}",
                )
            )
        await s.commit()

    r = await web_client.get("/")
    assert r.status_code == 200
    assert "HTTP 502 from server" in r.text
    assert "3" in r.text  # item count
    assert "failed" in r.text


# --- Create / update / pause / unpause / delete ------------------------------


@pytest.mark.asyncio
async def test_create_feed_form(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    r = await web_client.post(
        "/ui/feeds",
        data={"url": "https://example.com/", "title": "Example"},
    )
    assert r.status_code == 303
    assert "/ui/feeds/" in r.headers["location"]

    async with api_factory() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(Feed))).scalars().all()
        assert len(rows) == 1
        assert rows[0].title == "Example"


@pytest.mark.asyncio
async def test_create_feed_form_default_title_falls_back_to_url(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    r = await web_client.post("/ui/feeds", data={"url": "https://example.com/"})
    assert r.status_code == 303
    async with api_factory() as s:
        from sqlalchemy import select

        feed = (await s.execute(select(Feed))).scalars().one()
        assert feed.title == "https://example.com/"


@pytest.mark.asyncio
async def test_create_feed_form_rejects_duplicate(
    web_client: httpx.AsyncClient,
) -> None:
    await web_client.post("/ui/feeds", data={"url": "https://example.com/"})
    r = await web_client.post("/ui/feeds", data={"url": "https://example.com/"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_update_feed_form(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Old")
        s.add(feed)
        await s.commit()
        feed_id = feed.id

    r = await web_client.post(
        f"/ui/feeds/{feed_id}/edit",
        data={"title": "New", "poll_interval_seconds": "1234"},
    )
    assert r.status_code == 303

    async with api_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.title == "New"
        assert feed.poll_interval_seconds == 1234


@pytest.mark.asyncio
async def test_pause_and_unpause(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.commit()
        feed_id = feed.id

    r = await web_client.post(f"/ui/feeds/{feed_id}/pause")
    assert r.status_code == 303
    async with api_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.status == "paused"

    # Mark a stale failure reason that unpause should clear.
    async with api_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        feed.last_failure_reason = "previously failed"
        await s.commit()

    r = await web_client.post(f"/ui/feeds/{feed_id}/unpause")
    assert r.status_code == 303
    async with api_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.status == "active"
        assert feed.last_failure_reason is None


@pytest.mark.asyncio
async def test_delete_form_requires_method_field(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.commit()
        feed_id = feed.id

    # Wrong _method value rejected.
    r = await web_client.post(
        f"/ui/feeds/{feed_id}/delete", data={"_method": "PATCH"}
    )
    assert r.status_code == 400

    # Either no _method or _method=DELETE are both accepted.
    r = await web_client.post(
        f"/ui/feeds/{feed_id}/delete", data={"_method": "DELETE"}
    )
    assert r.status_code == 303
    async with api_factory() as s:
        assert await s.get(Feed, feed_id) is None


@pytest.mark.asyncio
async def test_delete_unknown_feed_returns_404(
    web_client: httpx.AsyncClient,
) -> None:
    r = await web_client.post(
        f"/ui/feeds/{uuid.uuid4()}/delete", data={"_method": "DELETE"}
    )
    assert r.status_code == 404


# --- Feed detail -------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_detail_renders(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.flush()
        s.add(
            FeedItem(
                feed_id=feed.id,
                guid="g1",
                url="https://example.com/post-a",
                title="Post A",
            )
        )
        s.add(ScrapeRun(feed_id=feed.id, status="success"))
        await s.commit()
        feed_id = feed.id

    r = await web_client.get(f"/ui/feeds/{feed_id}")
    assert r.status_code == 200
    body = r.text
    assert "Example" in body
    assert "Post A" in body
    # Recent runs section should appear.
    assert "success" in body
    # Public RSS/Atom links surfaced.
    assert f"/feeds/{feed_id}/rss" in body
    assert f"/feeds/{feed_id}/atom" in body


@pytest.mark.asyncio
async def test_feed_detail_unknown_returns_404(
    web_client: httpx.AsyncClient,
) -> None:
    r = await web_client.get(f"/ui/feeds/{uuid.uuid4()}")
    assert r.status_code == 404


# --- Scrape trigger (depends on fake scraper + stub LLM) ---------------------


@pytest.mark.asyncio
async def test_scrape_form_triggers_run(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.commit()
        feed_id = feed.id

    fake_scraper.register("https://example.com/", html=HTML_BASIC)
    stub_llm.queue(XPATH)

    r = await web_client.post(f"/ui/feeds/{feed_id}/scrape")
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"/ui/feeds/{feed_id}")

    async with api_factory() as s:
        from sqlalchemy import select

        items = (
            await s.execute(select(FeedItem).where(FeedItem.feed_id == feed_id))
        ).scalars().all()
        assert len(items) == 2


@pytest.mark.asyncio
async def test_scrape_form_unknown_feed_404(
    web_client: httpx.AsyncClient,
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    r = await web_client.post(f"/ui/feeds/{uuid.uuid4()}/scrape")
    assert r.status_code == 404


# --- Static files ------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_css_served(web_client: httpx.AsyncClient) -> None:
    # StaticFiles is mounted outside the auth dependency.
    web_client.headers.pop("Authorization", None)
    r = await web_client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "Veilleur" in r.text


# Re-export to keep the import non-orphan for linters.
assert isinstance(TEST_BEARER_TOKEN, str)
