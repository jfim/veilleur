"""Unit tests for the RSS/Atom rendering helpers.

These exercise :mod:`veilleur.feeds.render` directly without spinning up the
FastAPI app — the route-level tests live in :mod:`tests.feeds.test_routes`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import lxml.etree as etree

from veilleur.db.models import Feed, FeedItem
from veilleur.feeds.render import render_atom, render_rss

NS_ATOM = {"atom": "http://www.w3.org/2005/Atom"}


def _make_feed(*, item_count: int = 3) -> tuple[Feed, list[FeedItem]]:
    feed_id = uuid.uuid4()
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    feed = Feed(
        id=feed_id,
        url="https://example.com/blog",
        title="Example Blog",
        poll_interval_seconds=3600,
        status="active",
        created_at=now - timedelta(days=10),
        updated_at=now,
    )
    items: list[FeedItem] = []
    for i in range(item_count):
        items.append(
            FeedItem(
                id=uuid.uuid4(),
                feed_id=feed_id,
                guid=f"guid-{i}",
                url=f"https://example.com/blog/post-{i}",
                title=f"Post {i}",
                summary=f"Summary {i}" if i % 2 == 0 else None,
                first_seen_at=now - timedelta(hours=i),
                last_seen_at=now - timedelta(hours=i),
            )
        )
    return feed, items


def test_render_rss_well_formed_with_entries() -> None:
    feed, items = _make_feed(item_count=3)
    body = render_rss(feed, items)
    root = etree.fromstring(body)
    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("title") == "Example Blog"
    assert channel.findtext("link") == "https://example.com/blog"
    rss_items = channel.findall("item")
    assert len(rss_items) == 3
    titles = [it.findtext("title") for it in rss_items]
    # Newest first.
    assert titles == ["Post 0", "Post 1", "Post 2"]
    # Each entry has a link.
    for it in rss_items:
        assert it.findtext("link", "").startswith("https://example.com/blog/post-")


def test_render_atom_well_formed_with_entries() -> None:
    feed, items = _make_feed(item_count=2)
    body = render_atom(feed, items)
    root = etree.fromstring(body)
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    assert root.find("atom:title", NS_ATOM).text == "Example Blog"  # type: ignore[union-attr]
    entries = root.findall("atom:entry", NS_ATOM)
    assert len(entries) == 2
    titles = [e.findtext("atom:title", namespaces=NS_ATOM) for e in entries]
    assert titles == ["Post 0", "Post 1"]
    for e in entries:
        link = e.find("atom:link", NS_ATOM)
        assert link is not None
        assert link.get("href", "").startswith("https://example.com/blog/post-")


def test_render_handles_empty_items() -> None:
    feed, _ = _make_feed(item_count=0)
    rss = render_rss(feed, [])
    atom = render_atom(feed, [])
    rss_root = etree.fromstring(rss)
    atom_root = etree.fromstring(atom)
    channel = rss_root.find("channel")
    assert channel is not None
    assert channel.findall("item") == []
    assert atom_root.findall("atom:entry", NS_ATOM) == []


def test_render_rss_uses_published_then_first_seen() -> None:
    feed, items = _make_feed(item_count=1)
    items[0].published_at = datetime(2026, 4, 1, tzinfo=UTC)
    body = render_rss(feed, items)
    root = etree.fromstring(body)
    pub = root.find("channel/item/pubDate")
    assert pub is not None
    assert pub.text is not None
    # RSS pubDate is RFC 822; check year + month token.
    assert "Apr 2026" in pub.text or "01 Apr" in pub.text
