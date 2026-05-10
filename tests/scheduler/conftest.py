"""Scheduler-test fixtures.

The scheduler queries Postgres directly, so its tests share the
session-scoped engine set up in the top-level ``conftest.py`` and
TRUNCATE between tests like the pipeline / API suites do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def scheduler_factory(
    alembic_upgraded: None, pg_engine: AsyncEngine
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real ``async_sessionmaker`` against the shared test engine."""
    factory = async_sessionmaker(bind=pg_engine, expire_on_commit=False, class_=AsyncSession)

    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE feed_items, scrape_runs, xpath_extractors, feeds"
                " RESTART IDENTITY CASCADE"
            )
        )

    yield factory
