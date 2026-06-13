"""Background scheduler loop.

Single asyncio task that, on each tick, queries for the next feed whose
``last_scraped_at + poll_interval_seconds < now()`` and calls the supplied
``scrape`` callable on it. All scrape failures are caught and logged; the
loop never dies on its own.

Graceful shutdown uses an ``asyncio.Event`` rather than task cancellation
so an in-flight scrape can finish naturally — the global
:class:`asyncio.Lock` inside :func:`veilleur.pipeline.run_scrape` already
ensures only one scrape runs at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed
from veilleur.pipeline import STALE_RUN_TIMEOUT, ScrapeOutcome, recover_stale_runs

logger = logging.getLogger(__name__)


ScrapeCallable = Callable[[uuid.UUID], Awaitable[ScrapeOutcome]]


async def pick_due_feed(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID | None:
    """Return the id of the next ``active`` feed that is due, or ``None``.

    A feed is due when it has never been scraped, or when
    ``last_scraped_at + poll_interval_seconds * 1 second`` is in the past.
    Feeds never scraped sort first; otherwise the oldest
    ``last_scraped_at`` wins.
    """
    interval = func.make_interval(0, 0, 0, 0, 0, 0, Feed.poll_interval_seconds)
    stmt = (
        select(Feed.id)
        .where(Feed.status == "active")
        .where(
            or_(
                Feed.last_scraped_at.is_(None),
                Feed.last_scraped_at + interval < func.now(),
            )
        )
        .order_by(Feed.last_scraped_at.asc().nullsfirst())
        .limit(1)
    )
    async with session_factory() as session:
        return (await session.execute(stmt)).scalar_one_or_none()


class SchedulerLoop:
    """Long-running task that drives scrapes off the ``feeds`` table."""

    def __init__(
        self,
        *,
        scrape: ScrapeCallable,
        session_factory: async_sessionmaker[AsyncSession],
        tick_seconds: float = 30.0,
        stale_run_timeout: timedelta = STALE_RUN_TIMEOUT,
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        self._scrape = scrape
        self._session_factory = session_factory
        self._tick_seconds = tick_seconds
        self._stale_run_timeout = stale_run_timeout
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Spawn the background task. Idempotent if already running."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="veilleur-scheduler")

    async def stop(self) -> None:
        """Signal shutdown and wait for the loop (and any in-flight scrape)."""
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        try:
            await task
        finally:
            self._task = None

    async def tick(self) -> uuid.UUID | None:
        """Run one iteration: pick a due feed, scrape if any. Returns the id."""
        try:
            await recover_stale_runs(self._session_factory, older_than=self._stale_run_timeout)
        except Exception:
            logger.exception("scheduler: stale-run sweep failed")
        try:
            feed_id = await pick_due_feed(self._session_factory)
        except Exception:
            logger.exception("scheduler: pick_due_feed failed")
            return None
        if feed_id is None:
            return None
        try:
            await self._scrape(feed_id)
        except Exception:
            # Defense in depth: run_scrape converts most failures to a
            # ``ScrapeOutcome``, but anything that escapes must not kill
            # the loop.
            logger.exception("scheduler: scrape raised for feed %s", feed_id)
        return feed_id

    async def _run(self) -> None:
        logger.info("scheduler started (tick=%.1fs)", self._tick_seconds)
        try:
            while not self._stop_event.is_set():
                feed_id = await self.tick()
                if self._stop_event.is_set():
                    break
                if feed_id is None:
                    await self._sleep_or_stop(self._tick_seconds)
        finally:
            logger.info("scheduler stopped")

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep up to ``seconds`` but return early if stop is signalled."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
