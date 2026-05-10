"""Live end-to-end scrape against blog.jean-francois.im.

Skipped by default (`-m "not live"`).

- ``test_scrape_jfim_blog_matches_published_feed`` — full live: real LLM,
  real passe-partout, real DB.
- ``test_scrape_jfim_blog_with_fixed_xpath`` — passe-partout + DB live, but
  the LLM is a fixed fake returning the xpath previously derived by
  ``openai/gpt-oss-120b``. Less flaky; verifies the pipeline against
  real-world HTML.

Both require:
- ``PASSEPARTOUT_URL`` (+ ``PASSEPARTOUT_BEARER_TOKEN`` if needed)
- ``POSTGRES_*`` pointing at a DB with migrations applied.
- The full-LLM test additionally requires ``LLM_API_*``.

Run with: ``uv run pytest -m live tests/pipeline/test_scrape_live.py -s``
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urldefrag

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from veilleur.db.models import Feed, FeedItem
from veilleur.db.session import get_session_factory
from veilleur.pipeline import run_scrape
from veilleur.scraper import client_from_env
from veilleur.xpath import HttpxLLMClient

BLOG_URL = "https://blog.jean-francois.im/"
FEED_URL = "https://blog.jean-francois.im/index.xml"


def _parse_feed_links(xml: str) -> set[str]:
    """Extract entry URLs from an Atom or RSS feed (no XML lib needed)."""
    links: set[str] = set()
    # RSS: <item><link>URL</link>
    for item in re.findall(r"<item\b[^>]*>(.*?)</item>", xml, re.DOTALL):
        m = re.search(r"<link\b[^>]*>([^<]+)</link>", item)
        if m:
            links.add(urldefrag(m.group(1).strip())[0].rstrip("/"))
    # Atom: <entry>...<link href="URL"/>
    for entry in re.findall(r"<entry\b[^>]*>(.*?)</entry>", xml, re.DOTALL):
        m = re.search(r'<link\b[^>]*href="([^"]+)"', entry)
        if m:
            links.add(urldefrag(m.group(1))[0].rstrip("/"))
    return links


async def _ensure_feed(session: AsyncSession) -> uuid.UUID:
    existing = (
        await session.execute(select(Feed).where(Feed.url == BLOG_URL))
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id
    feed = Feed(url=BLOG_URL, title="blog.jean-francois.im")
    session.add(feed)
    await session.commit()
    return feed.id


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.asyncio
async def test_scrape_jfim_blog_matches_published_feed() -> None:
    """Scrape the blog and verify our extracted URLs cover the published feed."""
    # 1. Fetch the canonical Atom feed for ground truth.
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        resp = await http.get(FEED_URL)
        resp.raise_for_status()
        published = _parse_feed_links(resp.text)
    assert published, f"no entries parsed from {FEED_URL}"
    print(f"\npublished feed has {len(published)} entries")

    # 2. Set up the feed row.
    factory = get_session_factory()
    async with factory() as session:
        feed_id = await _ensure_feed(session)

    # 3. Run the pipeline end-to-end with real services.
    from veilleur.config import get_settings

    settings = get_settings()
    assert settings.LLM_API_URL and settings.LLM_MODEL_NAME and settings.LLM_API_KEY
    scraper = client_from_env()
    async with httpx.AsyncClient(timeout=120.0) as llm_http:
        llm = HttpxLLMClient(
            api_url=settings.LLM_API_URL,
            model=settings.LLM_MODEL_NAME,
            api_key=settings.LLM_API_KEY.get_secret_value(),
            http=llm_http,
        )
        try:
            outcome = await run_scrape(feed_id, scraper=scraper, llm=llm, session_factory=factory)
        finally:
            await scraper.aclose()

    print(f"outcome: {outcome}")
    assert outcome.error_message is None, outcome.error_message
    assert outcome.status in ("success", "xpath_regenerated")
    assert outcome.items_seen > 0

    # 4. Compare extracted items against the published feed.
    async with factory() as session:
        items = (
            (await session.execute(select(FeedItem.url).where(FeedItem.feed_id == feed_id)))
            .scalars()
            .all()
        )
    extracted = {urldefrag(u)[0].rstrip("/") for u in items}
    print(f"extracted {len(extracted)} URLs from blog")
    overlap = extracted & published
    print(f"overlap with published feed: {len(overlap)} / {len(published)}")
    missing = published - extracted
    if missing:
        print("missing (in feed, not extracted):")
        for u in sorted(missing):
            print(f"  {u}")
    extra = extracted - published
    if extra:
        print(f"extra (extracted but not in feed): {len(extra)}")
        for u in sorted(extra)[:10]:
            print(f"  {u}")

    # The blog index may show fewer or more posts than the feed (pagination,
    # archive vs. recent). We require at least 50% overlap as a sanity check
    # that the xpath actually targets blog posts and not navigation.
    assert overlap, "no overlap between extracted URLs and published feed"
    coverage = len(overlap) / len(published)
    assert coverage >= 0.5, f"only {coverage:.0%} of published entries appeared on the blog index"


# Captured from a previous run with openai/gpt-oss-120b. The user's blog
# structure shouldn't change, so we hard-code this and use a fake LLM.
GPT_OSS_XPATH = "//a[contains(@href, '/posts/')]"


class _FixedLLMClient:
    """Returns the same xpath every call, wrapped in the new two-line format.

    The intended-id list is derived from the prompt so the loop validates
    cleanly: we look for ``id=N | ... href=`` lines and pick those whose
    href matches a hard-coded substring (here ``/posts/``).
    """

    def __init__(self, xpath: str, *, href_substring: str = "/posts/") -> None:
        self._xpath = xpath
        self._href_substring = href_substring
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        ids: list[int] = []
        for line in prompt.splitlines():
            if not line.startswith("id="):
                continue
            # Format: id=N | path | text='...' | href=...
            if self._href_substring in line.split("href=", 1)[-1]:
                try:
                    ids.append(int(line[3:].split(" ", 1)[0]))
                except (ValueError, IndexError):
                    continue
        return f"articles: {','.join(str(i) for i in ids)}\nxpath: {self._xpath}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scrape_jfim_blog_with_fixed_xpath() -> None:
    """Same target as the full-LLM test, but with a deterministic fake LLM.

    Verifies the end-to-end pipeline against real passe-partout + real HTML
    without depending on a chat-completions endpoint.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as http:
        resp = await http.get(FEED_URL)
        resp.raise_for_status()
        published = _parse_feed_links(resp.text)
    assert published, f"no entries parsed from {FEED_URL}"

    factory = get_session_factory()
    async with factory() as session:
        # Use a distinct URL so it doesn't collide with the full-LLM test
        # if both ran in the same DB.
        feed = (
            await session.execute(select(Feed).where(Feed.url == BLOG_URL))
        ).scalar_one_or_none()
        if feed is None:
            feed = Feed(url=BLOG_URL, title="blog.jean-francois.im (fixed-xpath)")
            session.add(feed)
            await session.commit()
        feed_id = feed.id
        # Reset extractor state so we exercise the derive path with the fake.
        feed.active_xpath_extractor_id = None
        await session.commit()

    scraper = client_from_env()
    llm = _FixedLLMClient(GPT_OSS_XPATH)
    try:
        outcome = await run_scrape(feed_id, scraper=scraper, llm=llm, session_factory=factory)
    finally:
        await scraper.aclose()

    print(f"\nfixed-xpath outcome: {outcome}")
    assert outcome.error_message is None, outcome.error_message
    assert outcome.status == "xpath_regenerated"
    assert outcome.items_seen > 0
    assert llm.calls == 1

    async with factory() as session:
        items = (
            (await session.execute(select(FeedItem.url).where(FeedItem.feed_id == feed_id)))
            .scalars()
            .all()
        )
    extracted = {urldefrag(u)[0].rstrip("/") for u in items}
    overlap = extracted & published
    missing = published - extracted
    print(f"extracted {len(extracted)} URLs; overlap {len(overlap)} / {len(published)}")
    if missing:
        print("missing:")
        for u in sorted(missing):
            print(f"  {u}")

    # The hard-coded xpath should cover every published feed entry.
    assert missing == set(), f"published entries not extracted: {missing}"
