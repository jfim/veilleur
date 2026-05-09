"""Render :class:`Feed` rows + their items as RSS 2.0 / Atom 1.0 documents.

Pure functions: take ORM rows in, return XML bytes out. The HTTP layer in
:mod:`veilleur.feeds.routes` is responsible for status codes, content types,
and caching headers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from feedgen.feed import FeedGenerator  # type: ignore[import-untyped]

from veilleur.db.models import Feed, FeedItem

MAX_ITEMS = 50


def _entry_id(feed: Feed, item: FeedItem) -> str:
    return f"urn:veilleur:feed:{feed.id}:item:{item.guid}"


def _build_generator(feed: Feed, items: list[FeedItem]) -> FeedGenerator:
    fg = FeedGenerator()
    feed_url = feed.url
    fg.id(f"urn:veilleur:feed:{feed.id}")
    fg.title(feed.title)
    fg.link(href=feed_url, rel="alternate")
    fg.description(feed.title)
    fg.language("en")

    latest_update = max(
        (item.last_seen_at for item in items),
        default=feed.updated_at,
    )
    fg.updated(_ensure_aware(latest_update))

    # feedgen orders entries newest-last in output; iterate oldest-first so
    # the rendered feed has the most recent item at the top.
    for item in reversed(items):
        fe = fg.add_entry()
        fe.id(_entry_id(feed, item))
        fe.title(item.title)
        fe.link(href=item.url, rel="alternate")
        if item.summary:
            fe.description(item.summary)
            fe.content(item.summary)
        published = item.published_at or item.first_seen_at
        fe.published(_ensure_aware(published))
        fe.updated(_ensure_aware(item.last_seen_at))
    return fg


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def render_rss(feed: Feed, items: list[FeedItem]) -> bytes:
    """Render the feed and items as RSS 2.0 XML bytes."""
    fg = _build_generator(feed, items)
    return fg.rss_str(pretty=True)


def render_atom(feed: Feed, items: list[FeedItem]) -> bytes:
    """Render the feed and items as Atom 1.0 XML bytes."""
    fg = _build_generator(feed, items)
    return fg.atom_str(pretty=True)
