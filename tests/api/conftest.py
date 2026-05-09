"""API-test fixtures.

Like the pipeline tests, these run against the session-scoped Postgres
testcontainer and TRUNCATE between tests — FastAPI route handlers and the
scrape pipeline both open their own committed transactions, so the
SAVEPOINT-based isolation in the top-level ``conftest.py`` doesn't fit.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from veilleur.scraper import FakePassePartout

TEST_BEARER_TOKEN = "test-bearer-token"


class StubLLMClient:
    """Returns canned replies in order; configurable per-test."""

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls: list[str] = []

    def queue(self, *replies: str) -> None:
        self.replies.extend(replies)

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self.replies:
            raise AssertionError("StubLLMClient ran out of replies")
        return self.replies.pop(0)


@pytest.fixture
def configure_api_token(alembic_upgraded: None) -> Iterator[None]:
    """Set ``VEILLEUR_API_BEARER_TOKEN`` for the duration of a test."""
    old = os.environ.get("VEILLEUR_API_BEARER_TOKEN")
    os.environ["VEILLEUR_API_BEARER_TOKEN"] = TEST_BEARER_TOKEN
    from veilleur.config import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("VEILLEUR_API_BEARER_TOKEN", None)
        else:
            os.environ["VEILLEUR_API_BEARER_TOKEN"] = old
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def api_factory(
    alembic_upgraded: None, pg_container: PostgresContainer
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Truncating session factory shared between pipeline and FastAPI."""
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


@pytest_asyncio.fixture
async def api_app(
    configure_api_token: None,
    api_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[FastAPI]:
    """Reset the lazy global engine so ``run_scrape`` and the route's session
    dependency both bind to the testcontainer."""
    from veilleur.db import session as db_session

    db_session._engine = None  # type: ignore[attr-defined]
    db_session._session_factory = None  # type: ignore[attr-defined]

    from veilleur.app import app

    yield app

    db_session._engine = None  # type: ignore[attr-defined]
    db_session._session_factory = None  # type: ignore[attr-defined]
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_BEARER_TOKEN}"},
    ) as client:
        yield client


@pytest.fixture
def fake_scraper(api_app: FastAPI) -> FakePassePartout:
    from veilleur.api.deps import get_scraper

    scraper = FakePassePartout()
    api_app.dependency_overrides[get_scraper] = lambda: scraper
    return scraper


@pytest.fixture
def stub_llm(api_app: FastAPI) -> StubLLMClient:
    from veilleur.api.deps import get_llm_client

    llm = StubLLMClient()
    api_app.dependency_overrides[get_llm_client] = lambda: llm
    return llm
