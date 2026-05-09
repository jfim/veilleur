"""End-to-end tests for the public RSS/Atom routes (Phase 7).

The fixtures from :mod:`tests.api.conftest` (FastAPI app + truncating session
factory) work for these endpoints too — the public router is mounted on the
same FastAPI app — so tests live alongside the REST API tests.
"""

from __future__ import annotations

import uuid

import httpx
import lxml.etree as etree
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, FeedItem
from veilleur.feeds.render import MAX_ITEMS

NS_ATOM = {"atom": "http://www.w3.org/2005/Atom"}


async def _create_feed_with_items(
    factory: async_sessionmaker[AsyncSession], *, item_count: int
) -> uuid.UUID:
    async with factory() as s:
        feed = Feed(
            url="https://example.com/blog",
            title="Example Blog",
            poll_interval_seconds=3600,
        )
        s.add(feed)
        await s.flush()
        for i in range(item_count):
            s.add(
                FeedItem(
                    feed_id=feed.id,
                    guid=f"g{i}",
                    url=f"https://example.com/blog/post-{i}",
                    title=f"Post {i}",
                    summary=f"Summary {i}",
                )
            )
        await s.commit()
        return feed.id


@pytest.mark.asyncio
async def test_rss_endpoint_does_not_require_auth(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _create_feed_with_items(api_factory, item_count=2)
    api_client.headers.pop("Authorization", None)
    r = await api_client.get(f"/feeds/{feed_id}/rss")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    assert "max-age=300" in r.headers.get("cache-control", "")
    root = etree.fromstring(r.content)
    items = root.findall("channel/item")
    assert len(items) == 2


@pytest.mark.asyncio
async def test_atom_endpoint_does_not_require_auth(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _create_feed_with_items(api_factory, item_count=2)
    api_client.headers.pop("Authorization", None)
    r = await api_client.get(f"/feeds/{feed_id}/atom")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/atom+xml")
    assert "max-age=300" in r.headers.get("cache-control", "")
    root = etree.fromstring(r.content)
    entries = root.findall("atom:entry", NS_ATOM)
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_rss_unknown_feed_returns_404(api_client: httpx.AsyncClient) -> None:
    api_client.headers.pop("Authorization", None)
    r = await api_client.get(f"/feeds/{uuid.uuid4()}/rss")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_atom_unknown_feed_returns_404(api_client: httpx.AsyncClient) -> None:
    api_client.headers.pop("Authorization", None)
    r = await api_client.get(f"/feeds/{uuid.uuid4()}/atom")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_rss_caps_items_at_max(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _create_feed_with_items(api_factory, item_count=MAX_ITEMS + 5)
    api_client.headers.pop("Authorization", None)
    r = await api_client.get(f"/feeds/{feed_id}/rss")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    items = root.findall("channel/item")
    assert len(items) == MAX_ITEMS


@pytest.mark.asyncio
async def test_rss_empty_feed_renders_valid_xml(
    api_client: httpx.AsyncClient,
    api_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _create_feed_with_items(api_factory, item_count=0)
    api_client.headers.pop("Authorization", None)
    r = await api_client.get(f"/feeds/{feed_id}/rss")
    assert r.status_code == 200
    root = etree.fromstring(r.content)
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == "Example Blog"
    assert channel.findall("item") == []
