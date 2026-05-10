"""Pipeline-test fixtures.

The pipeline opens its own sessions internally (it commits intermediate state
before doing long-running fetch/LLM calls), so the SAVEPOINT-based rollback in
``tests/conftest.py`` doesn't fit. Instead we share the session-scoped engine
and TRUNCATE the relevant tables between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def pipeline_factory(
    alembic_upgraded: None, pg_engine: AsyncEngine
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real ``async_sessionmaker`` against the shared test engine."""
    factory = async_sessionmaker(bind=pg_engine, expire_on_commit=False, class_=AsyncSession)

    # Wipe between tests. Order matters: feeds has the deferrable FK to
    # xpath_extractors, so TRUNCATE CASCADE handles the dependency edges.
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE feed_items, scrape_runs, xpath_extractors, feeds"
                " RESTART IDENTITY CASCADE"
            )
        )

    yield factory
