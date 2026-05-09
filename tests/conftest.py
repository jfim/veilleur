"""Pytest fixtures for Veilleur.

Spins up a Postgres container once per session, applies Alembic migrations once,
then hands each test an ``AsyncSession`` bound to a SAVEPOINT so changes are
rolled back on teardown.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from alembic import command


def _set_pg_env(pg: PostgresContainer) -> dict[str, str | None]:
    keys = (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    )
    old: dict[str, str | None] = {k: os.environ.get(k) for k in keys}
    os.environ["POSTGRES_HOST"] = str(pg.get_container_host_ip())
    os.environ["POSTGRES_PORT"] = str(pg.get_exposed_port(5432))
    os.environ["POSTGRES_USER"] = pg.username
    os.environ["POSTGRES_PASSWORD"] = pg.password
    os.environ["POSTGRES_DB"] = pg.dbname
    return old


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def alembic_upgraded(pg_container: PostgresContainer) -> Iterator[None]:
    """Run Alembic migrations once per session against the testcontainer."""
    old = _set_pg_env(pg_container)

    from veilleur.config import get_settings
    from veilleur.db import session as db_session

    get_settings.cache_clear()
    db_session._engine = None  # type: ignore[attr-defined]
    db_session._session_factory = None  # type: ignore[attr-defined]

    sync_url = (
        f"postgresql+psycopg://{pg_container.username}:{pg_container.password}"
        f"@{pg_container.get_container_host_ip()}:{pg_container.get_exposed_port(5432)}"
        f"/{pg_container.dbname}"
    )
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")

    yield

    for key, val in old.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


@pytest_asyncio.fixture
async def db_session(
    alembic_upgraded: None, pg_container: PostgresContainer
) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession with SAVEPOINT-based rollback isolation.

    Implements the canonical SQLAlchemy "join an external transaction" recipe:
    open an outer transaction, start a SAVEPOINT, and restart the SAVEPOINT
    after each commit so that user-level commits never persist across tests.
    """
    async_url = (
        f"postgresql+asyncpg://{pg_container.username}:{pg_container.password}"
        f"@{pg_container.get_container_host_ip()}:{pg_container.get_exposed_port(5432)}"
        f"/{pg_container.dbname}"
    )
    engine = create_async_engine(async_url, future=True)
    connection = await engine.connect()
    outer = await connection.begin()
    await connection.begin_nested()

    session = AsyncSession(bind=connection, expire_on_commit=False)

    @event.listens_for(session.sync_session, "after_transaction_end")
    def _restart_savepoint(sess: object, trans: object) -> None:  # noqa: ARG001  # pyright: ignore[reportUnusedFunction]
        sync_conn = connection.sync_connection
        if sync_conn is None:
            return
        if not sync_conn.in_nested_transaction() and sync_conn.in_transaction():
            sync_conn.begin_nested()

    try:
        yield session
    finally:
        await session.close()
        if outer.is_active:
            await outer.rollback()
        await connection.close()
        await engine.dispose()
