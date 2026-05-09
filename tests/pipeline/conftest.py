"""Pipeline-test fixtures.

The pipeline opens its own sessions internally (it commits intermediate state
before doing long-running fetch/LLM calls), so the SAVEPOINT-based rollback in
``tests/conftest.py`` doesn't fit. Instead we share the session-scoped Postgres
testcontainer and TRUNCATE the relevant tables between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]


@pytest_asyncio.fixture
async def pipeline_factory(
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

    # Wipe between tests. Order matters: feeds has the deferrable FK to
    # xpath_extractors, so TRUNCATE CASCADE handles the dependency edges.
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
