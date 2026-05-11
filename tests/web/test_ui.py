"""Phase 9 web UI tests.

Drives the templated routes through ``httpx.AsyncClient`` against the
FastAPI app, sharing the existing ``api_factory`` / ``api_app`` fixtures
in ``tests/api/conftest.py`` so we get the same testcontainer-backed DB,
truncate-between-tests semantics, and bearer-token configuration.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, FeedItem, ScrapeRun, XPathExtractor
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
# HTML_BASIC has 2 anchors → ids 1, 2; XPATH matches both.
XPATH_REPLY = f"articles: 1,2\nxpath: {XPATH}"


SESSION_COOKIE = "veilleur_session"
CSRF_COOKIE = "veilleur_csrf"
CSRF_TOKEN = "test-csrf-token"


@pytest_asyncio.fixture
async def web_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE: TEST_BEARER_TOKEN, CSRF_COOKIE: CSRF_TOKEN},
        # Send the matching CSRF token on every request via the header
        # alternative so individual tests don't have to bake it into each
        # form payload.
        headers={"X-CSRF-Token": CSRF_TOKEN},
        follow_redirects=False,
    ) as client:
        yield client


# --- Auth --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_redirects_to_login_when_unauthenticated(
    web_client: httpx.AsyncClient,
) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.get("/")
    assert r.status_code == 303
    assert r.headers["Location"].startswith("/login")


@pytest.mark.asyncio
async def test_index_preserves_next_on_redirect(web_client: httpx.AsyncClient) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.get("/ui/settings/prompt")
    assert r.status_code == 303
    assert r.headers["Location"] == "/login?next=/ui/settings/prompt"


@pytest.mark.asyncio
async def test_wrong_cookie_rejected(web_client: httpx.AsyncClient) -> None:
    web_client.cookies.set(SESSION_COOKIE, "not-the-token")
    r = await web_client.get("/")
    assert r.status_code == 303
    assert r.headers["Location"].startswith("/login")


@pytest.mark.asyncio
async def test_login_form_renders(web_client: httpx.AsyncClient) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.get("/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert 'name="remember_me"' in r.text


@pytest.mark.asyncio
async def test_login_submit_sets_cookie_and_redirects(web_client: httpx.AsyncClient) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.post(
        "/login",
        data={"password": TEST_BEARER_TOKEN, "next": "/"},
    )
    assert r.status_code == 303
    assert r.headers["Location"] == "/"
    assert SESSION_COOKIE in r.cookies
    assert r.cookies[SESSION_COOKIE] == TEST_BEARER_TOKEN


@pytest.mark.asyncio
async def test_login_submit_with_remember_me_sets_max_age(
    web_client: httpx.AsyncClient,
) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.post(
        "/login",
        data={"password": TEST_BEARER_TOKEN, "remember_me": "1", "next": "/"},
    )
    assert r.status_code == 303
    set_cookie = r.headers["set-cookie"].lower()
    assert "max-age=" in set_cookie


@pytest.mark.asyncio
async def test_login_submit_rejects_bad_password(web_client: httpx.AsyncClient) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.post("/login", data={"password": "wrong", "next": "/"})
    assert r.status_code == 401
    assert "Invalid password" in r.text
    assert SESSION_COOKIE not in r.cookies


@pytest.mark.asyncio
async def test_login_submit_rejects_open_redirect(web_client: httpx.AsyncClient) -> None:
    web_client.cookies.delete(SESSION_COOKIE)
    r = await web_client.post(
        "/login",
        data={"password": TEST_BEARER_TOKEN, "next": "//evil.example/x"},
    )
    assert r.status_code == 303
    assert r.headers["Location"] == "/"


@pytest.mark.asyncio
async def test_logout_clears_cookie(web_client: httpx.AsyncClient) -> None:
    r = await web_client.post("/logout")
    assert r.status_code == 303
    assert r.headers["Location"] == "/login"
    set_cookie = r.headers["set-cookie"].lower()
    assert SESSION_COOKIE in set_cookie
    # delete_cookie sets Max-Age=0 (or an expired date)
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


@pytest.mark.asyncio
async def test_unset_token_rejects_web_ui(api_app: FastAPI, web_client: httpx.AsyncClient) -> None:
    from veilleur.config import get_settings

    old = os.environ.pop("API_BEARER_TOKEN", None)
    get_settings.cache_clear()
    try:
        r = await web_client.get("/")
        assert r.status_code == 503
    finally:
        if old is not None:
            os.environ["API_BEARER_TOKEN"] = old
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
    r = await web_client.post(f"/ui/feeds/{feed_id}/delete", data={"_method": "PATCH"})
    assert r.status_code == 400

    # Either no _method or _method=DELETE are both accepted.
    r = await web_client.post(f"/ui/feeds/{feed_id}/delete", data={"_method": "DELETE"})
    assert r.status_code == 303
    async with api_factory() as s:
        assert await s.get(Feed, feed_id) is None


@pytest.mark.asyncio
async def test_delete_unknown_feed_returns_404(
    web_client: httpx.AsyncClient,
) -> None:
    r = await web_client.post(f"/ui/feeds/{uuid.uuid4()}/delete", data={"_method": "DELETE"})
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
    stub_llm.queue(XPATH_REPLY)

    r = await web_client.post(f"/ui/feeds/{feed_id}/scrape")
    assert r.status_code == 303
    assert f"/ui/feeds/{feed_id}" in r.headers["location"]

    async with api_factory() as s:
        from sqlalchemy import select

        items = (
            (await s.execute(select(FeedItem).where(FeedItem.feed_id == feed_id))).scalars().all()
        )
        assert len(items) == 2


@pytest.mark.asyncio
async def test_scrape_form_unknown_feed_404(
    web_client: httpx.AsyncClient,
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    r = await web_client.post(f"/ui/feeds/{uuid.uuid4()}/scrape")
    assert r.status_code == 404


# --- Regenerate xpath --------------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_xpath_form_replaces_active_extractor(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    # Seed a feed with an existing (deliberately wrong) xpath, so we can
    # verify that regenerate swaps in a new extractor row even though the
    # existing one still parses cleanly.
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.flush()
        old = XPathExtractor(
            feed_id=feed.id,
            xpath="//main//article//h2/a[contains(@href, '/wrong/')]",
            generated_by="llm",
            llm_model="stub",
        )
        s.add(old)
        await s.flush()
        feed.active_xpath_extractor_id = old.id
        await s.commit()
        feed_id = feed.id
        old_id = old.id

    fake_scraper.register("https://example.com/", html=HTML_BASIC)
    stub_llm.queue(XPATH_REPLY)

    r = await web_client.post(f"/ui/feeds/{feed_id}/regenerate-xpath")
    assert r.status_code == 303
    assert f"/ui/feeds/{feed_id}" in r.headers["location"]

    async with api_factory() as s:
        from sqlalchemy import select

        refreshed = (await s.execute(select(Feed).where(Feed.id == feed_id))).scalar_one()
        assert refreshed.active_xpath_extractor_id is not None
        assert refreshed.active_xpath_extractor_id != old_id

        new_extractor = (
            await s.execute(
                select(XPathExtractor).where(
                    XPathExtractor.id == refreshed.active_xpath_extractor_id
                )
            )
        ).scalar_one()
        assert new_extractor.xpath == XPATH

        items = (
            (await s.execute(select(FeedItem).where(FeedItem.feed_id == feed_id))).scalars().all()
        )
        assert len(items) == 2

        runs = (
            (await s.execute(select(ScrapeRun).where(ScrapeRun.feed_id == feed_id))).scalars().all()
        )
        assert len(runs) == 1
        assert runs[0].status == "xpath_regenerated"


@pytest.mark.asyncio
async def test_regenerate_xpath_form_unknown_feed_404(
    web_client: httpx.AsyncClient,
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    r = await web_client.post(f"/ui/feeds/{uuid.uuid4()}/regenerate-xpath")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_feed_detail_shows_run_xpath(
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
    stub_llm.queue(XPATH_REPLY)

    # Trigger a scrape to populate a run with an xpath_extractor row.
    r = await web_client.post(f"/ui/feeds/{feed_id}/scrape")
    assert r.status_code == 303

    detail = await web_client.get(f"/ui/feeds/{feed_id}")
    assert detail.status_code == 200
    # The xpath should be visible inside the runs table cell.
    assert XPATH in detail.text


# --- Prompt settings ---------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_settings_disabled_when_unset(
    web_client: httpx.AsyncClient,
) -> None:
    from veilleur.config import get_settings

    old = os.environ.pop("PROMPT_FILE", None)
    get_settings.cache_clear()
    try:
        r = await web_client.get("/ui/settings/prompt")
        assert r.status_code == 200
        assert "is not configured" in r.text

        save = await web_client.post("/ui/settings/prompt", data={"prompt": "anything"})
        assert save.status_code == 409
    finally:
        if old is not None:
            os.environ["PROMPT_FILE"] = old
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_prompt_settings_save_and_reset(
    web_client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    from veilleur.config import get_settings
    from veilleur.xpath import prompt as xpath_prompt

    target = tmp_path / "prompt.txt"
    old = os.environ.get("PROMPT_FILE")
    os.environ["PROMPT_FILE"] = str(target)
    get_settings.cache_clear()
    try:
        r = await web_client.get("/ui/settings/prompt")
        assert r.status_code == 200
        assert "using bundled default" in r.text

        custom = "CUSTOM-PROMPT {title} {url}\n{listing}\n"
        save = await web_client.post("/ui/settings/prompt", data={"prompt": custom})
        assert save.status_code == 200
        assert target.read_text(encoding="utf-8") == custom
        assert "Prompt saved" in save.text

        revert = await web_client.post(
            "/ui/settings/prompt",
            data={"prompt": xpath_prompt.DEFAULT_PROMPT_TEMPLATE},
        )
        assert revert.status_code == 200
        assert not target.exists()
        assert "override file removed" in revert.text

        xpath_prompt.save_template("override-again")
        assert target.exists()
        reset = await web_client.post("/ui/settings/prompt/reset")
        assert reset.status_code == 303
        assert not target.exists()
    finally:
        if old is None:
            os.environ.pop("PROMPT_FILE", None)
        else:
            os.environ["PROMPT_FILE"] = old
        get_settings.cache_clear()


# --- Static files ------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_css_served(web_client: httpx.AsyncClient) -> None:
    # StaticFiles is mounted outside the auth dependency.
    web_client.headers.pop("Authorization", None)
    r = await web_client.get("/static/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "Veilleur" in r.text


# --- Issue #39 / #40 / #41: scrape-run UX ------------------------------------


@pytest.mark.asyncio
async def test_scrape_form_redirects_with_queued_flash(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    """`Scrape now` returns a 303 with a flash=Scrape+queued query string."""
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.commit()
        feed_id = feed.id

    fake_scraper.register("https://example.com/", html=HTML_BASIC)
    stub_llm.queue(XPATH_REPLY)

    r = await web_client.post(f"/ui/feeds/{feed_id}/scrape")
    assert r.status_code == 303
    location = r.headers["location"]
    assert f"/ui/feeds/{feed_id}" in location
    assert "flash=Scrape" in location
    assert "flash_kind=success" in location

    # Following the redirect renders the flash banner.
    follow = await web_client.get(location)
    assert follow.status_code == 200
    assert "Scrape queued." in follow.text
    assert 'class="flash success"' in follow.text


@pytest.mark.asyncio
async def test_scrape_form_dedupes_when_already_running(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
    fake_scraper: FakePassePartout,
    stub_llm: StubLLMClient,
) -> None:
    """A second scrape click while one is in flight returns a busy flash and
    does not enqueue a new run."""
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.flush()
        # Pre-existing in-flight run blocks the new request.
        running = ScrapeRun(feed_id=feed.id, status="running")
        s.add(running)
        await s.commit()
        feed_id = feed.id

    r = await web_client.post(f"/ui/feeds/{feed_id}/scrape")
    assert r.status_code == 303
    location = r.headers["location"]
    assert "flash=A%20scrape%20is%20already%20running" in location
    assert "flash_kind=info" in location

    from sqlalchemy import select

    async with api_factory() as s:
        runs = (
            (await s.execute(select(ScrapeRun).where(ScrapeRun.feed_id == feed_id))).scalars().all()
        )
        assert len(runs) == 1  # No new run was queued.


@pytest.mark.asyncio
async def test_index_shows_failed_reprocessing_pill(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed feed with an in-flight ScrapeRun renders ``failed (reprocessing)``
    and the page sets a meta-refresh."""
    async with api_factory() as s:
        feed = Feed(
            url="https://example.com/",
            title="Example",
            status="failed",
            last_failure_reason="prior failure",
        )
        s.add(feed)
        await s.flush()
        s.add(ScrapeRun(feed_id=feed.id, status="running", current_step="fetching"))
        await s.commit()

    r = await web_client.get("/")
    assert r.status_code == 200
    assert "failed" in r.text
    assert "(reprocessing)" in r.text
    assert 'http-equiv="refresh"' in r.text


@pytest.mark.asyncio
async def test_feed_detail_shows_current_step_and_meta_refresh(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An in-flight scrape on the feed-detail page shows the current step
    name and triggers a meta-refresh."""
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.flush()
        s.add(
            ScrapeRun(
                feed_id=feed.id,
                status="running",
                current_step="deriving_xpath",
            )
        )
        await s.commit()
        feed_id = feed.id

    r = await web_client.get(f"/ui/feeds/{feed_id}")
    assert r.status_code == 200
    assert "Scrape in progress" in r.text
    assert "deriving_xpath" in r.text
    assert 'http-equiv="refresh"' in r.text


@pytest.mark.asyncio
async def test_feed_detail_no_meta_refresh_when_idle(
    web_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without an in-flight scrape, the page does not auto-refresh."""
    async with api_factory() as s:
        feed = Feed(url="https://example.com/", title="Example")
        s.add(feed)
        await s.commit()
        feed_id = feed.id

    r = await web_client.get(f"/ui/feeds/{feed_id}")
    assert r.status_code == 200
    assert 'http-equiv="refresh"' not in r.text
    assert "Scrape in progress" not in r.text


# Re-export to keep the import non-orphan for linters.
assert isinstance(TEST_BEARER_TOKEN, str)
