"""Pytest fixtures for Veilleur.

Spins up a Postgres container once per session, applies Alembic migrations
once, then hands each test either an ``AsyncSession`` bound to a SAVEPOINT
(rolled back on teardown) or a session factory backed by a shared engine
(per-test TRUNCATE).

Set ``VEILLEUR_TEST_REUSE_POSTGRES=1`` (with the standard ``POSTGRES_*`` env
vars pointing at an already-running Postgres) to skip the testcontainer.
That's what CI uses — the workflow already runs a Postgres service container.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from alembic import command


@dataclass(frozen=True)
class PgInfo:
    host: str
    port: int
    user: str
    password: str
    db: str

    @property
    def sync_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.db}"
        )

    @property
    def async_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.db}"
        )


def _set_pg_env(info: PgInfo) -> dict[str, str | None]:
    keys = (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    )
    old: dict[str, str | None] = {k: os.environ.get(k) for k in keys}
    os.environ["POSTGRES_HOST"] = info.host
    os.environ["POSTGRES_PORT"] = str(info.port)
    os.environ["POSTGRES_USER"] = info.user
    os.environ["POSTGRES_PASSWORD"] = info.password
    os.environ["POSTGRES_DB"] = info.db
    return old


@pytest.fixture(scope="session")
def pg_info() -> Iterator[PgInfo]:
    if os.environ.get("VEILLEUR_TEST_REUSE_POSTGRES") == "1":
        yield PgInfo(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            user=os.environ.get("POSTGRES_USER", "veilleur"),
            password=os.environ.get("POSTGRES_PASSWORD", "veilleur"),
            db=os.environ.get("POSTGRES_DB", "veilleur"),
        )
        return
    with PostgresContainer("postgres:16-alpine") as pg:
        yield PgInfo(
            host=str(pg.get_container_host_ip()),
            port=int(pg.get_exposed_port(5432)),
            user=pg.username,
            password=pg.password,
            db=pg.dbname,
        )


@pytest.fixture(autouse=True, scope="session")
def stub_required_env() -> Iterator[None]:
    """Set sentinel values for env vars the app refuses to start without.

    The lifespan handler validates ``API_BEARER_TOKEN`` (or ``API_AUTH_DISABLED``),
    ``PASSEPARTOUT_URL``, and the LLM credential trio. Tests that use
    ``TestClient`` would otherwise fail to boot. Tests can still override
    individual values with ``monkeypatch``.
    """
    defaults = {
        "API_BEARER_TOKEN": "test-bearer-token",
        "PASSEPARTOUT_URL": "http://passe-partout.test",
        "LLM_API_URL": "http://llm.test/v1",
        "LLM_MODEL_NAME": "test-model",
        "LLM_API_KEY": "test-key",
    }
    old = {k: os.environ.get(k) for k in defaults}
    for k, v in defaults.items():
        os.environ.setdefault(k, v)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="session")
def alembic_upgraded(pg_info: PgInfo) -> Iterator[None]:
    """Run Alembic migrations once per session against the test Postgres."""
    old = _set_pg_env(pg_info)

    from veilleur.config import get_settings
    from veilleur.db import session as db_session

    get_settings.cache_clear()
    db_session._engine = None  # type: ignore[attr-defined]
    db_session._session_factory = None  # type: ignore[attr-defined]

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_info.sync_url)
    command.upgrade(cfg, "head")

    yield

    for key, val in old.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def pg_engine(
    alembic_upgraded: None, pg_info: PgInfo
) -> AsyncIterator[AsyncEngine]:
    """One async engine for the whole test session.

    Per-test fixtures check out connections from this engine's pool instead of
    each spinning up their own engine — that avoids the asyncpg handshake on
    every test, which is the dominant per-test overhead.
    """
    engine = create_async_engine(pg_info.async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(pg_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Per-test AsyncSession with SAVEPOINT-based rollback isolation.

    Implements the canonical SQLAlchemy "join an external transaction" recipe:
    open an outer transaction, start a SAVEPOINT, and restart the SAVEPOINT
    after each commit so that user-level commits never persist across tests.
    """
    connection = await pg_engine.connect()
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
