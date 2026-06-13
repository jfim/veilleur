"""Tests for orphaned-run recovery (startup reset + staleness sweep)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from veilleur.db.models import Feed, ScrapeRun
from veilleur.pipeline import recover_stale_runs


async def _make_feed(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with factory() as session:
        feed = Feed(url="https://example.com/", title="Test", status="active")
        session.add(feed)
        await session.commit()
        return feed.id


async def _make_run(
    factory: async_sessionmaker[AsyncSession],
    feed_id: uuid.UUID,
    *,
    status: str,
    started_at: datetime | None = None,
    current_step: str | None = None,
) -> uuid.UUID:
    async with factory() as session:
        run = ScrapeRun(
            feed_id=feed_id,
            status=status,
            current_step=current_step,
            finished_at=None if status == "running" else datetime.now(UTC),
        )
        if started_at is not None:
            run.started_at = started_at
        session.add(run)
        await session.commit()
        return run.id


async def _get_run(factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> ScrapeRun:
    async with factory() as session:
        run = await session.get(ScrapeRun, run_id)
        assert run is not None
        return run


@pytest.mark.asyncio
async def test_startup_recovery_marks_running_run_failed(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """older_than=None resets every running run and clears its in-flight step."""
    feed_id = await _make_feed(pipeline_factory)
    run_id = await _make_run(
        pipeline_factory, feed_id, status="running", current_step="deriving_xpath"
    )

    count = await recover_stale_runs(pipeline_factory)

    assert count == 1
    run = await _get_run(pipeline_factory, run_id)
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.current_step is None
    assert run.error_message is not None


@pytest.mark.asyncio
async def test_recovery_ignores_terminal_runs(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """success / failed / xpath_regenerated runs are never touched."""
    feed_id = await _make_feed(pipeline_factory)
    terminal_ids = [
        await _make_run(pipeline_factory, feed_id, status=s)
        for s in ("success", "failed", "xpath_regenerated")
    ]

    count = await recover_stale_runs(pipeline_factory)

    assert count == 0
    for run_id, expected in zip(
        terminal_ids, ("success", "failed", "xpath_regenerated"), strict=True
    ):
        run = await _get_run(pipeline_factory, run_id)
        assert run.status == expected


@pytest.mark.asyncio
async def test_sweep_only_resets_runs_older_than_cutoff(
    pipeline_factory: async_sessionmaker[AsyncSession],
) -> None:
    """older_than leaves genuinely in-flight runs alone, fails the stale one."""
    feed_id = await _make_feed(pipeline_factory)
    now = datetime.now(UTC)
    fresh = await _make_run(
        pipeline_factory, feed_id, status="running", started_at=now - timedelta(minutes=2)
    )
    stale = await _make_run(
        pipeline_factory, feed_id, status="running", started_at=now - timedelta(minutes=20)
    )

    count = await recover_stale_runs(pipeline_factory, older_than=timedelta(minutes=15))

    assert count == 1
    assert (await _get_run(pipeline_factory, fresh)).status == "running"
    assert (await _get_run(pipeline_factory, stale)).status == "failed"
