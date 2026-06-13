"""Tests for the background scheduler loop."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, ScrapeRun
from veilleur.pipeline import ScrapeOutcome
from veilleur.scheduler import SchedulerLoop, pick_due_feed


async def _make_feed(
    factory: async_sessionmaker[AsyncSession],
    *,
    url: str = "https://example.com/",
    title: str = "Test",
    status: str = "active",
    poll_interval_seconds: int = 60,
    last_scraped_at: datetime | None = None,
) -> uuid.UUID:
    async with factory() as session:
        feed = Feed(
            url=url,
            title=title,
            status=status,
            poll_interval_seconds=poll_interval_seconds,
            last_scraped_at=last_scraped_at,
        )
        session.add(feed)
        await session.commit()
        return feed.id


def _ok_outcome(scrape_run_id: uuid.UUID | None = None) -> ScrapeOutcome:
    return ScrapeOutcome(
        scrape_run_id=scrape_run_id or uuid.uuid4(),
        status="success",
        items_seen=0,
        items_new=0,
        error_message=None,
    )


# --- pick_due_feed -----------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_due_feed_returns_none_when_empty(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await pick_due_feed(scheduler_factory) is None


@pytest.mark.asyncio
async def test_pick_due_feed_picks_never_scraped(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _make_feed(scheduler_factory)
    assert await pick_due_feed(scheduler_factory) == feed_id


@pytest.mark.asyncio
async def test_pick_due_feed_skips_paused(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_feed(scheduler_factory, status="paused")
    assert await pick_due_feed(scheduler_factory) is None


@pytest.mark.asyncio
async def test_pick_due_feed_skips_failed(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_feed(scheduler_factory, status="failed")
    assert await pick_due_feed(scheduler_factory) is None


@pytest.mark.asyncio
async def test_pick_due_feed_respects_interval(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A feed scraped 5s ago with a 3600s interval is not yet due."""
    recent = datetime.now(UTC) - timedelta(seconds=5)
    await _make_feed(
        scheduler_factory,
        url="https://example.com/recent",
        poll_interval_seconds=3600,
        last_scraped_at=recent,
    )
    assert await pick_due_feed(scheduler_factory) is None


@pytest.mark.asyncio
async def test_pick_due_feed_picks_oldest_due(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When multiple feeds are due, the one with the oldest last_scraped_at wins."""
    now = datetime.now(UTC)
    older = await _make_feed(
        scheduler_factory,
        url="https://example.com/older",
        poll_interval_seconds=10,
        last_scraped_at=now - timedelta(seconds=600),
    )
    await _make_feed(
        scheduler_factory,
        url="https://example.com/newer",
        poll_interval_seconds=10,
        last_scraped_at=now - timedelta(seconds=60),
    )
    assert await pick_due_feed(scheduler_factory) == older


@pytest.mark.asyncio
async def test_pick_due_feed_prefers_never_scraped_over_due(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    fresh = await _make_feed(scheduler_factory, url="https://example.com/fresh")
    await _make_feed(
        scheduler_factory,
        url="https://example.com/old",
        poll_interval_seconds=10,
        last_scraped_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    assert await pick_due_feed(scheduler_factory) == fresh


# --- SchedulerLoop.tick ------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_scrapes_due_feed(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _make_feed(scheduler_factory)
    calls: list[uuid.UUID] = []

    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        calls.append(fid)
        return _ok_outcome()

    loop = SchedulerLoop(scrape=fake_scrape, session_factory=scheduler_factory, tick_seconds=1.0)
    result = await loop.tick()
    assert result == feed_id
    assert calls == [feed_id]


@pytest.mark.asyncio
async def test_tick_no_due_feed_no_scrape(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _make_feed(scheduler_factory, status="paused")
    calls: list[uuid.UUID] = []

    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        calls.append(fid)
        return _ok_outcome()

    loop = SchedulerLoop(scrape=fake_scrape, session_factory=scheduler_factory, tick_seconds=1.0)
    assert await loop.tick() is None
    assert calls == []


@pytest.mark.asyncio
async def test_tick_sweeps_stale_running_run(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each tick reaps runs that have been ``running`` past the staleness cutoff."""
    feed_id = await _make_feed(scheduler_factory, status="paused")  # no due feed to scrape
    async with scheduler_factory() as session:
        stale = ScrapeRun(
            feed_id=feed_id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        session.add(stale)
        await session.commit()
        stale_id = stale.id

    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        return _ok_outcome()

    loop = SchedulerLoop(
        scrape=fake_scrape,
        session_factory=scheduler_factory,
        tick_seconds=1.0,
        stale_run_timeout=timedelta(minutes=15),
    )
    await loop.tick()

    async with scheduler_factory() as session:
        run = await session.get(ScrapeRun, stale_id)
        assert run is not None
        assert run.status == "failed"


@pytest.mark.asyncio
async def test_tick_swallows_scrape_exception(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Defense in depth: if ``run_scrape`` raises, the scheduler must continue."""
    feed_id = await _make_feed(scheduler_factory)

    async def boom(fid: uuid.UUID) -> ScrapeOutcome:
        raise RuntimeError("kaboom")

    loop = SchedulerLoop(scrape=boom, session_factory=scheduler_factory, tick_seconds=1.0)
    # Should not raise — the loop logs and moves on.
    result = await loop.tick()
    assert result == feed_id


# --- SchedulerLoop start/stop lifecycle --------------------------------------


@pytest.mark.asyncio
async def test_start_stop_runs_at_least_one_scrape(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    feed_id = await _make_feed(scheduler_factory)
    seen = asyncio.Event()
    calls: list[uuid.UUID] = []

    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        calls.append(fid)
        seen.set()
        return _ok_outcome()

    loop = SchedulerLoop(scrape=fake_scrape, session_factory=scheduler_factory, tick_seconds=0.05)
    loop.start()
    try:
        await asyncio.wait_for(seen.wait(), timeout=2.0)
    finally:
        await loop.stop()

    assert calls and calls[0] == feed_id
    assert not loop.is_running


@pytest.mark.asyncio
async def test_stop_waits_for_in_flight_scrape(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``stop()`` must let the current scrape finish before returning."""
    await _make_feed(scheduler_factory)
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        started.set()
        await release.wait()
        finished.set()
        return _ok_outcome()

    loop = SchedulerLoop(scrape=slow_scrape, session_factory=scheduler_factory, tick_seconds=0.05)
    loop.start()
    await asyncio.wait_for(started.wait(), timeout=2.0)

    stop_task = asyncio.create_task(loop.stop())
    # Let the event loop turn so stop() actually starts awaiting.
    await asyncio.sleep(0.05)
    assert not stop_task.done(), "stop() returned before the in-flight scrape finished"
    assert not finished.is_set()

    release.set()
    await asyncio.wait_for(stop_task, timeout=2.0)
    assert finished.is_set()
    assert not loop.is_running


@pytest.mark.asyncio
async def test_start_is_idempotent(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        return _ok_outcome()

    loop = SchedulerLoop(scrape=fake_scrape, session_factory=scheduler_factory, tick_seconds=0.5)
    loop.start()
    assert loop.is_running
    # Calling start() again must not raise or spawn a second task.
    loop.start()
    assert loop.is_running
    await loop.stop()
    assert not loop.is_running


@pytest.mark.asyncio
async def test_stop_without_start_is_a_noop(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        return _ok_outcome()

    loop = SchedulerLoop(scrape=fake_scrape, session_factory=scheduler_factory, tick_seconds=0.5)
    await loop.stop()  # should not raise
    assert not loop.is_running


def test_init_rejects_non_positive_tick(
    scheduler_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def fake_scrape(fid: uuid.UUID) -> ScrapeOutcome:
        return _ok_outcome()

    with pytest.raises(ValueError):
        SchedulerLoop(scrape=fake_scrape, session_factory=scheduler_factory, tick_seconds=0.0)
