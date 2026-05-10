"""Unit-ish tests for the scrape pipeline using fakes + a real Postgres."""

from __future__ import annotations

import asyncio
import gzip
import uuid
from collections import deque
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, FeedItem, ScrapeRun, XPathExtractor
from veilleur.pipeline import run_scrape
from veilleur.scraper import FakePassePartout
from veilleur.xpath import LLMClient, XPathDerivationFailed

# A small HTML page with a clean blog-list structure that lxml can parse.
HTML_INITIAL = """
<!doctype html>
<html><head><title>Test Blog</title></head>
<body>
<header><nav>
  <a href="/about">About</a>
  <a href="/tags">Tags</a>
</nav></header>
<main>
  <article><h2><a href="/posts/2026/post-a">Post A</a></h2></article>
  <article><h2><a href="/posts/2026/post-b">Post B</a></h2></article>
  <article><h2><a href="/posts/2026/post-c">Post C</a></h2></article>
</main>
</body></html>
"""

# Same xpath still works on the second scrape — adds one new post + one repeat.
HTML_SECOND = """
<!doctype html>
<html><head><title>Test Blog</title></head>
<body>
<header><nav>
  <a href="/about">About</a>
  <a href="/tags">Tags</a>
</nav></header>
<main>
  <article><h2><a href="/posts/2026/post-d">Post D</a></h2></article>
  <article><h2><a href="/posts/2026/post-a">Post A</a></h2></article>
</main>
</body></html>
"""

# A page that breaks the previous xpath: links live under a different ancestor.
HTML_RESTRUCTURED = """
<!doctype html>
<html><head><title>Test Blog</title></head>
<body>
<div class="feed">
  <div class="entry"><a href="/posts/2026/post-a">Post A</a></div>
  <div class="entry"><a href="/posts/2026/post-b">Post B</a></div>
  <div class="entry"><a href="/posts/2026/post-c">Post C</a></div>
</div>
</body></html>
"""

XPATH_GOOD = "//main//article//h2/a"
XPATH_RESTRUCTURED = "//div[@class='feed']//a"


def _reply(intended_ids: list[int], xpath: str) -> str:
    """Build a model reply matching the new derive_xpath two-line format."""
    return f"articles: {','.join(str(i) for i in intended_ids)}\nxpath: {xpath}"


# Anchor ids for HTML_INITIAL after filtering: About=1, Tags=2, post-a=3,
# post-b=4, post-c=5. XPATH_GOOD matches the three post anchors.
REPLY_GOOD = _reply([3, 4, 5], XPATH_GOOD)

# HTML_RESTRUCTURED has only the three post anchors, ids 1, 2, 3.
REPLY_RESTRUCTURED = _reply([1, 2, 3], XPATH_RESTRUCTURED)


class FakeLLMClient:
    """Returns canned replies in order; raises when exhausted unless cycled."""

    def __init__(self, replies: list[str], *, cycle: bool = False) -> None:
        self._replies: deque[str] = deque(replies)
        self._cycle = cycle
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self._replies:
            raise AssertionError("FakeLLMClient ran out of replies")
        reply = self._replies.popleft()
        if self._cycle:
            self._replies.append(reply)
        return reply


_: type[LLMClient] = FakeLLMClient  # protocol conformance check


async def _make_feed(
    factory: async_sessionmaker[AsyncSession],
    *,
    url: str = "https://example.com/",
    title: str = "Test",
    status: str = "active",
    active_xpath: str | None = None,
) -> uuid.UUID:
    async with factory() as session:
        feed = Feed(url=url, title=title, status=status)
        session.add(feed)
        await session.flush()
        feed_id = feed.id
        if active_xpath is not None:
            extractor = XPathExtractor(
                feed_id=feed_id, xpath=active_xpath, generated_by="manual"
            )
            session.add(extractor)
            await session.flush()
            feed.active_xpath_extractor_id = extractor.id
        await session.commit()
        return feed_id


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_derives_xpath_and_persists_items(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No prior extractor → derive, apply, persist; status = xpath_regenerated."""
    feed_id = await _make_feed(pipeline_factory)
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)
    llm = FakeLLMClient([REPLY_GOOD])

    outcome = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )

    assert outcome.error_message is None
    assert outcome.status == "xpath_regenerated"
    assert outcome.items_seen == 3
    assert outcome.items_new == 3
    assert len(llm.calls) == 1

    async with pipeline_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.status == "active"
        assert feed.last_failure_reason is None
        assert feed.active_xpath_extractor_id is not None

        items = (
            (await s.execute(select(FeedItem).where(FeedItem.feed_id == feed_id)))
            .scalars()
            .all()
        )
        urls = {it.url for it in items}
        assert urls == {
            "https://example.com/posts/2026/post-a",
            "https://example.com/posts/2026/post-b",
            "https://example.com/posts/2026/post-c",
        }

        run = await s.get(ScrapeRun, outcome.scrape_run_id)
        assert run is not None
        assert run.status == "xpath_regenerated"
        assert run.items_seen == 3
        assert run.items_new == 3
        assert run.xpath_extractor_id == feed.active_xpath_extractor_id


@pytest.mark.asyncio
async def test_second_run_dedupes_and_updates_last_seen_at(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-scraping skips already-known items but bumps last_seen_at."""
    feed_id = await _make_feed(pipeline_factory, active_xpath=XPATH_GOOD)
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)
    llm = FakeLLMClient([])  # no derive expected

    first = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )
    assert first.status == "success"
    assert first.items_new == 3

    async with pipeline_factory() as s:
        before = (
            (
                await s.execute(
                    select(FeedItem.url, FeedItem.first_seen_at, FeedItem.last_seen_at)
                    .where(FeedItem.feed_id == feed_id)
                )
            )
            .all()
        )
    seen_map = {url: (first_seen, last_seen) for url, first_seen, last_seen in before}

    # Second scrape: same xpath, returns post-a (seen) and post-d (new).
    # (FakePassePartout.register overwrites)
    scraper.register("https://example.com/", html=HTML_SECOND)
    second = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )
    assert second.status == "success"
    assert second.items_seen == 2
    assert second.items_new == 1

    async with pipeline_factory() as s:
        after = (
            (
                await s.execute(
                    select(FeedItem.url, FeedItem.first_seen_at, FeedItem.last_seen_at)
                    .where(FeedItem.feed_id == feed_id)
                )
            )
            .all()
        )
    by_url = {url: (first_seen, last_seen) for url, first_seen, last_seen in after}
    post_a = "https://example.com/posts/2026/post-a"
    post_d = "https://example.com/posts/2026/post-d"
    assert post_d in by_url and post_d not in seen_map
    assert by_url[post_a][0] == seen_map[post_a][0]  # first_seen unchanged
    assert by_url[post_a][1] > seen_map[post_a][1]   # last_seen advanced


@pytest.mark.asyncio
async def test_regenerate_when_xpath_breaks(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Existing xpath fails → derive new → succeed; new extractor recorded."""
    feed_id = await _make_feed(pipeline_factory, active_xpath=XPATH_GOOD)

    # First run, prime prev_links so validation has something to compare to.
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)
    llm_noop = FakeLLMClient([])
    first = await run_scrape(
        feed_id, scraper=scraper, llm=llm_noop, session_factory=pipeline_factory
    )
    assert first.status == "success"

    # Second: page restructured, old xpath matches nothing, regenerate gets called.
    # (FakePassePartout.register overwrites)
    scraper.register("https://example.com/", html=HTML_RESTRUCTURED)
    llm = FakeLLMClient([REPLY_RESTRUCTURED])

    second = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )
    assert second.error_message is None
    assert second.status == "xpath_regenerated"
    assert len(llm.calls) == 1

    async with pipeline_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        extractor_count = (
            (
                await s.execute(
                    select(XPathExtractor).where(XPathExtractor.feed_id == feed_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(extractor_count) == 2
        assert feed.active_xpath_extractor is not None
        assert feed.active_xpath_extractor.xpath == XPATH_RESTRUCTURED


@pytest.mark.asyncio
async def test_paused_feed_is_skipped(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _make_feed(pipeline_factory, status="paused")
    scraper = FakePassePartout()
    llm = FakeLLMClient([])

    outcome = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )
    assert outcome.skipped is True
    assert outcome.status == "skipped"

    async with pipeline_factory() as s:
        runs = (
            (await s.execute(select(ScrapeRun).where(ScrapeRun.feed_id == feed_id)))
            .scalars()
            .all()
        )
    assert runs == []


@pytest.mark.asyncio
async def test_fetch_failure_marks_feed_failed(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    from veilleur.scraper import PassePartoutUnavailable

    feed_id = await _make_feed(pipeline_factory)
    scraper = FakePassePartout()
    scraper.register_error(
        "https://example.com/", PassePartoutUnavailable("daemon offline")
    )
    llm = FakeLLMClient([])

    outcome = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )
    assert outcome.status == "failed"
    assert outcome.error_message and "daemon offline" in outcome.error_message

    async with pipeline_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.status == "failed"
        assert feed.last_failure_reason is not None
        assert "daemon offline" in feed.last_failure_reason


@pytest.mark.asyncio
async def test_derive_failure_marks_feed_failed(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _make_feed(pipeline_factory)  # no extractor yet
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)

    class GiveUpLLM:
        async def complete(self, prompt: str) -> str:
            raise XPathDerivationFailed("LLM returned 'unable'")

    outcome = await run_scrape(
        feed_id, scraper=scraper, llm=GiveUpLLM(), session_factory=pipeline_factory
    )
    assert outcome.status == "failed"
    assert outcome.error_message and "xpath derivation failed" in outcome.error_message

    async with pipeline_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.status == "failed"


@pytest.mark.asyncio
async def test_regeneration_fail_marks_feed_failed(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Old xpath broken AND regenerated xpath also doesn't validate → fail."""
    feed_id = await _make_feed(pipeline_factory, active_xpath=XPATH_GOOD)

    # Prime prev_links.
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)
    primed = await run_scrape(
        feed_id,
        scraper=scraper,
        llm=FakeLLMClient([]),
        session_factory=pipeline_factory,
    )
    assert primed.status == "success"

    # Now a page where prev_xpath gets nothing, AND the regenerated xpath
    # returns a totally different host so validation rejects it.
    # (FakePassePartout.register overwrites)
    scraper.register(
        "https://example.com/",
        html=(
            "<html><head><title>x</title></head>"
            "<body><div class='feed'>"
            "<a href='https://other.example.org/foo'>foo</a>"
            "<a href='https://other.example.org/bar'>bar</a>"
            "</div></body></html>"
        ),
    )
    # Page has 2 anchors → ids 1, 2; xpath matches both.
    llm = FakeLLMClient([_reply([1, 2], "//div[@class='feed']//a")])

    outcome = await run_scrape(
        feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
    )
    assert outcome.status == "failed"
    assert outcome.error_message and "regenerated xpath" in outcome.error_message


@pytest.mark.asyncio
async def test_recovery_clears_failure_reason(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Successful scrape after a failure clears feed.status and last_failure_reason."""
    from veilleur.scraper import PassePartoutUnavailable

    feed_id = await _make_feed(pipeline_factory)
    scraper = FakePassePartout()
    scraper.register_error("https://example.com/", PassePartoutUnavailable("nope"))
    bad = await run_scrape(
        feed_id,
        scraper=scraper,
        llm=FakeLLMClient([]),
        session_factory=pipeline_factory,
    )
    assert bad.status == "failed"

    # (FakePassePartout.register overwrites)
    scraper.register("https://example.com/", html=HTML_INITIAL)
    good = await run_scrape(
        feed_id,
        scraper=scraper,
        llm=FakeLLMClient([REPLY_GOOD]),
        session_factory=pipeline_factory,
    )
    assert good.error_message is None
    assert good.status == "xpath_regenerated"

    async with pipeline_factory() as s:
        feed = await s.get(Feed, feed_id)
        assert feed is not None
        assert feed.status == "active"
        assert feed.last_failure_reason is None


@pytest.mark.asyncio
async def test_raw_html_written_to_disk_when_configured(
    pipeline_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RAW_HTML_DIR is set, raw HTML is gzipped to disk and only the path
    is recorded on the scrape_run row."""
    from veilleur.config import get_settings
    from veilleur.scraper import read_html

    monkeypatch.setenv("RAW_HTML_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        feed_id = await _make_feed(pipeline_factory)
        scraper = FakePassePartout()
        scraper.register("https://example.com/", html=HTML_INITIAL)
        llm = FakeLLMClient([REPLY_GOOD])

        outcome = await run_scrape(
            feed_id, scraper=scraper, llm=llm, session_factory=pipeline_factory
        )
        assert outcome.error_message is None

        async with pipeline_factory() as s:
            run = await s.get(ScrapeRun, outcome.scrape_run_id)
            assert run is not None
            assert run.raw_html_path is not None
            stored = run.raw_html_path

        on_disk = tmp_path / stored
        assert on_disk.exists()
        with gzip.open(on_disk, "rb") as fh:
            assert fh.read().decode("utf-8") == HTML_INITIAL

        # And the public read_html helper roundtrips.
        assert read_html(raw_html_dir=tmp_path, stored_path=stored) == HTML_INITIAL
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_lock_does_not_span_llm_call(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A slow LLM call on one feed must not block another feed's fetch.

    The pipeline lock should cover only the passe-partout fetch; the LLM call
    runs without holding it. We arrange feed A's LLM call to block until
    feed B's fetch has happened — if the lock spanned the LLM call, this
    would deadlock.
    """
    feed_a = await _make_feed(
        pipeline_factory, url="https://a.example.com/", title="A"
    )
    feed_b = await _make_feed(
        pipeline_factory, url="https://b.example.com/", title="B"
    )

    scraper = FakePassePartout()
    scraper.register("https://a.example.com/", html=HTML_INITIAL)
    scraper.register("https://b.example.com/", html=HTML_INITIAL)

    b_fetch_done = asyncio.Event()

    class SlowLLM:
        """Returns XPATH_GOOD for feed A, but only after feed B has fetched."""

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, prompt: str) -> str:
            self.calls += 1
            # Wait for B's fetch to finish before letting A's LLM return.
            await asyncio.wait_for(b_fetch_done.wait(), timeout=5.0)
            return REPLY_GOOD

    class FastLLM:
        async def complete(self, prompt: str) -> str:
            return REPLY_GOOD

    slow = SlowLLM()
    fast = FastLLM()

    async def run_a() -> None:
        await run_scrape(
            feed_a, scraper=scraper, llm=slow, session_factory=pipeline_factory
        )

    async def run_b() -> None:
        # Give A a moment to enter its LLM call first.
        await asyncio.sleep(0.05)
        try:
            await run_scrape(
                feed_b, scraper=scraper, llm=fast, session_factory=pipeline_factory
            )
        finally:
            b_fetch_done.set()

    # If the lock spanned the LLM call, A would block in its LLM, B would
    # block on the lock, and asyncio.wait_for inside SlowLLM would time out.
    await asyncio.wait_for(asyncio.gather(run_a(), run_b()), timeout=10.0)

    assert "https://a.example.com/" in scraper.calls
    assert "https://b.example.com/" in scraper.calls


# --- Issue #40: pipeline-step visibility -------------------------------------


@pytest.mark.asyncio
async def test_current_step_cleared_on_success(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A finalized successful run leaves ``current_step`` as NULL."""
    feed_id = await _make_feed(pipeline_factory)
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)

    outcome = await run_scrape(
        feed_id,
        scraper=scraper,
        llm=FakeLLMClient([REPLY_GOOD]),
        session_factory=pipeline_factory,
    )
    assert outcome.status == "xpath_regenerated"

    async with pipeline_factory() as s:
        run = await s.get(ScrapeRun, outcome.scrape_run_id)
        assert run is not None
        assert run.current_step is None


@pytest.mark.asyncio
async def test_current_step_cleared_on_failure(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A finalized failed run also leaves ``current_step`` as NULL."""
    from veilleur.scraper import PassePartoutUnavailable

    feed_id = await _make_feed(pipeline_factory)
    scraper = FakePassePartout()
    scraper.register_error(
        "https://example.com/", PassePartoutUnavailable("upstream down")
    )

    outcome = await run_scrape(
        feed_id,
        scraper=scraper,
        llm=FakeLLMClient([]),
        session_factory=pipeline_factory,
    )
    assert outcome.status == "failed"

    async with pipeline_factory() as s:
        run = await s.get(ScrapeRun, outcome.scrape_run_id)
        assert run is not None
        assert run.current_step is None


@pytest.mark.asyncio
async def test_current_step_set_during_in_flight_run(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A concurrent reader sees ``current_step`` populated while a slow LLM
    derivation holds the pipeline in the deriving_xpath stage."""
    feed_id = await _make_feed(pipeline_factory)
    scraper = FakePassePartout()
    scraper.register("https://example.com/", html=HTML_INITIAL)

    seen_step: list[str | None] = []
    derive_started = asyncio.Event()
    derive_release = asyncio.Event()

    class SteppedLLMClient:
        async def complete(self, prompt: str) -> str:
            derive_started.set()
            await derive_release.wait()
            return REPLY_GOOD

    async def watcher() -> None:
        await derive_started.wait()
        async with pipeline_factory() as s:
            row = (
                await s.execute(
                    select(ScrapeRun).where(ScrapeRun.feed_id == feed_id)
                )
            ).scalar_one()
            seen_step.append(row.current_step)
        derive_release.set()

    async def runner() -> None:
        await run_scrape(
            feed_id,
            scraper=scraper,
            llm=SteppedLLMClient(),
            session_factory=pipeline_factory,
        )

    await asyncio.wait_for(asyncio.gather(runner(), watcher()), timeout=10.0)
    assert seen_step == ["deriving_xpath"]

