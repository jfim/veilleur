"""Scheduler-test fixtures.

The scheduler queries Postgres directly, so its tests share the
session-scoped testcontainer set up in the top-level ``conftest.py`` and
TRUNCATE between tests like the pipeline / API suites do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]


@pytest_asyncio.fixture
async def scheduler_factory(
    alembic_upgraded: None, pg_container: PostgresContainer
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real ``async_sessionmaker`` against the testcontainer Postgres."""
    async_url = (
        f"postgresql+asyncpg://{pg_container.username}:{pg_container.password}"
        f"@{pg_container.get_container_host_ip()}:{pg_container.get_exposed_port(5432)}"
        f"/{pg_container.dbname}"
    )
    engine = create_async_engine(async_url, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE feed_items, scrape_runs, xpath_extractors, feeds"
                " RESTART IDENTITY CASCADE"
            )
        )

    try:
        yield factory
    finally:
        await engine.dispose()
