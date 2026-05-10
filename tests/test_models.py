"""Round-trip tests for the Phase 1 ORM models."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from veilleur.db.models import Feed, FeedItem, ScrapeRun, XPathExtractor


async def test_create_feed_with_extractor_round_trip(db_session: AsyncSession) -> None:
    """Insert a feed + its first xpath extractor in one transaction.

    The deferrable FK on ``feeds.active_xpath_extractor_id`` must allow the
    feed row to reference an extractor inserted later in the same transaction.
    """
    feed = Feed(url="https://example.com/blog", title="Example Blog")
    db_session.add(feed)
    await db_session.flush()  # gets feed.id without committing

    extractor = XPathExtractor(
        feed_id=feed.id,
        xpath="//a[@class='post']",
        generated_by="manual",
    )
    db_session.add(extractor)
    await db_session.flush()

    feed.active_xpath_extractor_id = extractor.id
    await db_session.flush()

    fetched = (await db_session.execute(select(Feed).where(Feed.id == feed.id))).scalar_one()
    assert fetched.url == "https://example.com/blog"
    assert fetched.active_xpath_extractor_id == extractor.id
    assert fetched.status == "active"
    assert fetched.poll_interval_seconds == 3600


async def test_feed_item_unique_per_feed_guid(db_session: AsyncSession) -> None:
    feed = Feed(url="https://example.com/a", title="A")
    db_session.add(feed)
    await db_session.flush()

    db_session.add(FeedItem(feed_id=feed.id, guid="g1", url="https://example.com/a/1", title="One"))
    await db_session.flush()

    db_session.add(
        FeedItem(feed_id=feed.id, guid="g1", url="https://example.com/a/1b", title="Dup")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_cascade_delete_removes_children(db_session: AsyncSession) -> None:
    feed = Feed(url="https://example.com/c", title="C")
    db_session.add(feed)
    await db_session.flush()

    extractor = XPathExtractor(feed_id=feed.id, xpath="//a", generated_by="manual")
    db_session.add(extractor)
    await db_session.flush()

    run = ScrapeRun(feed_id=feed.id, status="success")
    db_session.add(run)
    db_session.add(FeedItem(feed_id=feed.id, guid="g1", url="https://example.com/c/1", title="One"))
    await db_session.flush()

    await db_session.delete(feed)
    await db_session.flush()

    remaining_items = (
        await db_session.execute(select(FeedItem).where(FeedItem.feed_id == feed.id))
    ).all()
    remaining_runs = (
        await db_session.execute(select(ScrapeRun).where(ScrapeRun.feed_id == feed.id))
    ).all()
    remaining_ext = (
        await db_session.execute(select(XPathExtractor).where(XPathExtractor.feed_id == feed.id))
    ).all()
    assert remaining_items == []
    assert remaining_runs == []
    assert remaining_ext == []
