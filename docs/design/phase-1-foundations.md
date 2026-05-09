# Phase 1 — Foundations

> Database schema, Alembic migrations, dockerized Postgres for dev, config wiring.

This document is the authoritative spec for Phase 1. An implementer should be able to execute it without further design decisions.

---

## 1. Goals

- Define and create the four core tables (`feeds`, `feed_items`, `scrape_runs`, `xpath_extractors`) with UUIDv7 primary keys.
- Wire Alembic with autogenerate, naming conventions, and a single linear history.
- Provide a one-command dev Postgres via `docker-compose`.
- Centralize configuration in `veilleur/config.py` driven by env vars (with `.env` as a local-dev convenience only).
- Provide a `/healthz` endpoint that pings the local DB only.
- Provide a testcontainers-based pytest fixture skeleton with per-test transaction rollback.

## 2. Non-goals

- No scraping logic, XPath generation, RSS rendering, or feed polling.
- No retention pruner for `scrape_runs` (filed as follow-up; see §12).
- No passe-partout health probe in `/healthz` (managed separately).
- No auth, no migrations CLI beyond `alembic` itself.
- No data seed, no admin UI.

---

## 3. Schema

All primary keys and FKs are `UUID` (Postgres native `uuid` type). All timestamps are `TIMESTAMPTZ` and default to `now()`. All tables use the naming conventions in §6.

### 3.1 `xpath_extractors`

Versioned XPath rules. A feed points at exactly one row via `feeds.active_xpath_extractor_id`. History is preserved by inserting new rows; old rows are never mutated.

| column          | type          | constraints / notes                                                |
| --------------- | ------------- | ------------------------------------------------------------------ |
| `id`            | `UUID`        | PK, UUIDv7, generated app-side                                     |
| `feed_id`       | `UUID`        | NOT NULL, FK -> `feeds.id` ON DELETE CASCADE                       |
| `xpath`         | `TEXT`        | NOT NULL                                                           |
| `generated_by`  | `TEXT`        | NOT NULL; e.g. `"llm"`, `"manual"`                                 |
| `llm_model`     | `TEXT`        | NULL OK; populated when `generated_by = 'llm'`                     |
| `notes`         | `TEXT`        | NULL OK; freeform diagnostics / prompt summary                     |
| `created_at`    | `TIMESTAMPTZ` | NOT NULL, default `now()`                                          |

Indexes:
- `ix_xpath_extractors_feed_id_created_at` on `(feed_id, created_at DESC)` — list history per feed.

Note: there is intentionally **no** `is_active` column. Active selection lives on `feeds`.

### 3.2 `feeds`

| column                       | type          | constraints / notes                                              |
| ---------------------------- | ------------- | ---------------------------------------------------------------- |
| `id`                         | `UUID`        | PK, UUIDv7                                                       |
| `url`                        | `TEXT`        | NOT NULL, UNIQUE (`uq_feeds_url`)                                |
| `title`                      | `TEXT`        | NOT NULL                                                         |
| `poll_interval_seconds`      | `INTEGER`     | NOT NULL, default `3600`, CHECK (> 0)                            |
| `active_xpath_extractor_id`  | `UUID`        | NULL OK, FK -> `xpath_extractors.id` ON DELETE SET NULL, DEFERRABLE INITIALLY DEFERRED |
| `status`                     | `TEXT`        | NOT NULL, default `'active'`; CHECK in (`'active'`,`'paused'`,`'failed'`) |
| `last_scraped_at`            | `TIMESTAMPTZ` | NULL OK                                                          |
| `created_at`                 | `TIMESTAMPTZ` | NOT NULL, default `now()`                                        |
| `updated_at`                 | `TIMESTAMPTZ` | NOT NULL, default `now()`; updated app-side on write             |

Indexes:
- `ix_feeds_status_last_scraped_at` on `(status, last_scraped_at)` — for the future scheduler.

Circular FK note: `feeds` -> `xpath_extractors.id` and `xpath_extractors.feed_id` -> `feeds.id`. The FK on `feeds.active_xpath_extractor_id` is created `DEFERRABLE INITIALLY DEFERRED` so a feed and its first extractor can be inserted in a single transaction.

### 3.3 `feed_items`

| column         | type          | constraints / notes                                          |
| -------------- | ------------- | ------------------------------------------------------------ |
| `id`           | `UUID`        | PK, UUIDv7                                                   |
| `feed_id`      | `UUID`        | NOT NULL, FK -> `feeds.id` ON DELETE CASCADE                 |
| `guid`         | `TEXT`        | NOT NULL; site-supplied, else `sha256(url + "\x00" + title)` hex |
| `url`          | `TEXT`        | NOT NULL                                                     |
| `title`        | `TEXT`        | NOT NULL                                                     |
| `summary`      | `TEXT`        | NULL OK                                                      |
| `published_at` | `TIMESTAMPTZ` | NULL OK; site-supplied if available                          |
| `first_seen_at`| `TIMESTAMPTZ` | NOT NULL, default `now()`                                    |
| `scrape_run_id`| `UUID`        | NULL OK, FK -> `scrape_runs.id` ON DELETE SET NULL; the run that first observed this item |

Indexes:
- `uq_feed_items_feed_id_guid` UNIQUE on `(feed_id, guid)` — dedup key.
- `ix_feed_items_feed_id_first_seen_at` on `(feed_id, first_seen_at DESC)` — RSS rendering / `/items` listing.

Retention: never pruned (Phase 1 decision).

### 3.4 `scrape_runs`

| column             | type          | constraints / notes                                              |
| ------------------ | ------------- | ---------------------------------------------------------------- |
| `id`               | `UUID`        | PK, UUIDv7                                                       |
| `feed_id`          | `UUID`        | NOT NULL, FK -> `feeds.id` ON DELETE CASCADE                     |
| `xpath_extractor_id`| `UUID`       | NULL OK, FK -> `xpath_extractors.id` ON DELETE SET NULL          |
| `started_at`       | `TIMESTAMPTZ` | NOT NULL, default `now()`                                        |
| `finished_at`      | `TIMESTAMPTZ` | NULL OK                                                          |
| `status`           | `TEXT`        | NOT NULL; CHECK in (`'running'`,`'success'`,`'failed'`,`'xpath_regenerated'`) |
| `http_status`      | `INTEGER`     | NULL OK                                                          |
| `raw_html`         | `TEXT`        | NULL OK; the page source returned by passe-partout               |
| `items_seen`       | `INTEGER`     | NOT NULL, default `0`                                            |
| `items_new`        | `INTEGER`     | NOT NULL, default `0`                                            |
| `error_message`    | `TEXT`        | NULL OK                                                          |

Indexes:
- `ix_scrape_runs_feed_id_started_at` on `(feed_id, started_at DESC)`.
- `ix_scrape_runs_started_at` on `(started_at)` — supports the future 30-day pruner.

Retention: rows older than 30 days will be pruned by a follow-up job. Phase 1 only ensures the index exists.

---

## 4. UUIDv7 strategy

**Library: `uuid-utils`** (PyPI `uuid-utils`, Rust-backed, maintained, drop-in `uuid.UUID`-compatible). Chosen over `uuid7` because it is faster, actively maintained, and exposes a synchronous `uuid7()` returning a stdlib `uuid.UUID`.

- IDs are generated app-side (Python) — **not** in Postgres. Postgres 16 has no native `uuidv7()`; relying on it would force PG17+. App-side generation also keeps tests deterministic via monkeypatching.
- One helper: `veilleur/db/ids.py::new_id() -> uuid.UUID` wrapping `uuid_utils.uuid7()`.
- All SQLAlchemy models declare `id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=new_id)`.
- Time-ordered UUIDs play well with btree primary key inserts (avoids random-PK write amplification).

Add to `pyproject.toml`: `uuid-utils`.

---

## 5. Models module layout

```
veilleur/db/
├── __init__.py            # re-exports Base, engine, sessionmaker, get_session
├── base.py                # DeclarativeBase + MetaData(naming_convention=...)
├── ids.py                 # new_id() helper (uuid_utils.uuid7)
├── session.py             # async engine (asyncpg) + async_sessionmaker; get_session() dep
├── types.py               # type aliases: UUIDPk, TimestampTZ, etc. (optional, sugar)
└── models/
    ├── __init__.py        # imports each model so Alembic autogenerate sees them
    ├── feed.py            # Feed
    ├── feed_item.py       # FeedItem
    ├── scrape_run.py      # ScrapeRun
    └── xpath_extractor.py # XPathExtractor
```

Responsibilities:

- `base.py` — `class Base(DeclarativeBase): metadata = MetaData(naming_convention=NAMING_CONVENTION)`.
- `session.py` — builds the `AsyncEngine` from `settings.database_url_async`, exposes `async_session_factory` and a FastAPI dependency `async def get_session() -> AsyncIterator[AsyncSession]`.
- `models/*.py` — one ORM class per file, all subclass `Base`. Cross-model relationships use string references to avoid import cycles. The circular relationship between `Feed.active_xpath_extractor` and `XPathExtractor.feed` is configured with `foreign_keys=...` and `post_update=True` on the `Feed` side.
- `models/__init__.py` — `from .feed import Feed; from .feed_item import FeedItem; ...` so `Base.metadata` is fully populated when Alembic imports it.

---

## 6. Alembic config

Layout:

```
alembic.ini
alembic/
├── env.py
├── script.py.mako
└── versions/
```

`alembic.ini` highlights:
- `script_location = alembic`
- `sqlalchemy.url` is **not** set in the ini; `env.py` reads it from `settings.database_url_sync` (psycopg).
- `file_template = %%(year)d%%(month).2d%%(day).2d_%%(hour).2d%%(minute).2d_%%(rev)s_%%(slug)s`

`env.py`:
- Imports `veilleur.db.base.Base` and `veilleur.db.models` (side-effect imports to populate metadata).
- Sets `target_metadata = Base.metadata`.
- Sync engine using `psycopg` for migrations (`postgresql+psycopg://...`).
- Enables `compare_type=True` and `compare_server_default=True` in `context.configure`.

Naming convention (literal Python dict, lives in `veilleur/db/base.py`):

```python
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

Single linear history: no branches; CI enforces with `alembic check` and rejects multiple heads.

Initial migration: a single autogenerated revision creating all four tables, indexes, FKs, and CHECK constraints. The deferrable FK on `feeds.active_xpath_extractor_id` must be added with explicit `op.create_foreign_key(..., deferrable=True, initially="DEFERRED")` since autogen may not infer it.

---

## 7. Config schema (`veilleur/config.py`)

`pydantic-settings.BaseSettings` subclass. `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`. Env vars are the ground truth; `.env` is loaded only for local dev convenience and is gitignored.

| env var                     | type   | default                  | required | used by                                  |
| --------------------------- | ------ | ------------------------ | -------- | ---------------------------------------- |
| `LLM_API_URL`               | str    | —                        | yes      | XPath generation (later phase)           |
| `LLM_MODEL_NAME`            | str    | —                        | yes      | XPath generation (later phase)           |
| `LLM_API_KEY`               | secret | —                        | yes      | XPath generation (later phase)           |
| `PASSEPARTOUT_URL`          | str    | —                        | yes      | scraper (later phase)                    |
| `PASSEPARTOUT_BEARER_TOKEN` | secret | —                        | yes      | scraper (later phase)                    |
| `POSTGRES_HOST`             | str    | `localhost`              | no       | DB connection                            |
| `POSTGRES_PORT`             | int    | `5432`                   | no       | DB connection                            |
| `POSTGRES_USER`             | str    | `veilleur`               | no       | DB connection                            |
| `POSTGRES_PASSWORD`         | secret | `veilleur`               | no (dev) | DB connection                            |
| `POSTGRES_DB`               | str    | `veilleur`               | no       | DB connection                            |
| `LOG_LEVEL`                 | str    | `INFO`                   | no       | logging setup                            |
| `SCRAPE_DEFAULT_INTERVAL_SECONDS` | int | `3600`              | no       | feed default poll interval               |
| `SCRAPE_HTTP_TIMEOUT_SECONDS`     | int | `60`                | no       | passe-partout client (later phase)       |

Computed properties:
- `database_url_async` -> `postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}`
- `database_url_sync`  -> `postgresql+psycopg://{user}:{pw}@{host}:{port}/{db}`

Phase 1 only *requires* the Postgres vars at runtime. LLM/passe-partout vars are declared but only consumed in later phases — they should be `Optional[...]` in Phase 1 so the app boots without them, with a `model_validator` adding strict checks in later phases. (Alternative: keep them required and document that dev must export dummy values. Decision: optional in Phase 1.)

A single `@lru_cache` `get_settings()` returns the singleton.

---

## 8. `docker-compose.yml`

Lives at repo root.

```yaml
services:
  postgres:
    image: postgres:16-alpine
    container_name: veilleur-postgres
    environment:
      POSTGRES_USER: veilleur
      POSTGRES_PASSWORD: veilleur
      POSTGRES_DB: veilleur
    ports:
      - "5432:5432"
    volumes:
      - veilleur-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U veilleur -d veilleur"]
      interval: 5s
      timeout: 5s
      retries: 10

volumes:
  veilleur-pgdata:
```

No app container in Phase 1 — devs run FastAPI on the host against this DB.

---

## 9. Justfile additions

Append to existing `justfile`:

```just
# Bring up dev postgres
db-up:
    docker compose up -d postgres

# Tear down dev postgres (keeps volume)
db-down:
    docker compose down

# Generate a new migration: just db-migrate "add foo column"
db-migrate msg:
    uv run alembic revision --autogenerate -m "{{msg}}"

# Apply all migrations
db-upgrade:
    uv run alembic upgrade head

# Roll back one migration
db-downgrade:
    uv run alembic downgrade -1

# Drop the dev DB volume and re-run upgrade head
db-reset:
    docker compose down -v
    docker compose up -d postgres
    sleep 2
    uv run alembic upgrade head

# Verify metadata matches migrations (used in CI)
db-check:
    uv run alembic check
```

CI (`.github/workflows/ci.yml`) gains a job that spins up `postgres:16-alpine` as a service, runs `alembic upgrade head` then `alembic check`, and fails if either fails.

---

## 10. Test strategy

Stack: `pytest` + `pytest-asyncio` (already present) + `testcontainers[postgres]` (new dev dep).

Fixtures (`tests/conftest.py`, sketch):

```python
@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg

@pytest.fixture(scope="session")
def alembic_upgraded(pg_container) -> None:
    # Point alembic at pg_container.get_connection_url(); run upgrade head.
    ...

@pytest.fixture
async def db_session(alembic_upgraded, pg_container) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(pg_container.get_connection_url(driver="asyncpg"))
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(bind=conn, expire_on_commit=False) as s:
            nested = await conn.begin_nested()
            # SAVEPOINT-based per-test rollback pattern
            ...
            yield s
        await trans.rollback()
        await engine.dispose()
```

Per-test isolation via outer transaction + nested SAVEPOINT (the canonical SQLAlchemy "join an external transaction" recipe). Migrations run once per session.

Phase 1 test coverage:
- A smoke test that creates one row in each table (respecting FKs) and reads it back.
- A test that asserts `(feed_id, guid)` uniqueness on `feed_items`.
- A test that asserts cascade delete of a `feed` removes its `feed_items`, `scrape_runs`, and `xpath_extractors`.
- A test that `/healthz` returns 200 with a live DB and 503 when DB is unreachable.

Add `testcontainers[postgres]` to the `dev` dependency group.

---

## 11. `/healthz` behavior

Endpoint: `GET /healthz` in `veilleur/app.py` (replace the current stub).

Behavior:
- Acquires a session, runs `SELECT 1`.
- On success: `200` `{"status": "ok", "db": "ok"}`.
- On any DB exception: `503` `{"status": "degraded", "db": "<error class name>"}` (no error message body — avoid leaking).
- Does **not** probe passe-partout, the LLM endpoint, or any other dependency.
- Fast: must complete in < 100ms on a warm pool. No retries.

A separate `/livez` is *not* added in Phase 1 (filed as a follow-up if k8s-style separation is needed).

---

## 12. Open questions / deferred

- **`scrape_runs` pruner** — a daily job to delete rows older than 30 days. Deferred to a later phase; `ix_scrape_runs_started_at` exists to support it.
- **Raw HTML storage cost** — storing every run's HTML inline in `scrape_runs.raw_html` may grow fast. Phase 2 should evaluate (a) compressing with `pg_lz`/app-side gzip, (b) moving to object storage with only a pointer in the row, or (c) keeping HTML only for failed/regenerated runs.
- **Soft-delete on `feeds`** — currently hard-delete cascades. Revisit if users want "archive" semantics.
- **`updated_at` automation** — Phase 1 sets it app-side. A `BEFORE UPDATE` trigger is a possible Phase 2 addition for safety.
- **PG17 migration** — when PG17 is the floor, switch UUIDv7 generation to `gen_uuid_v7()` server-side and drop `uuid-utils`.
- **Multi-tenant / auth** — out of scope; revisit when the web UI lands.
- **Connection pool sizing** — defaults are fine for Phase 1's serial scraper. Tune when concurrency arrives (it won't in this codebase per README, but the API itself is concurrent).
- **`alembic check` false positives** — if autogen drifts on the deferrable FK, we may need a custom `compare_type` hook. Address if it bites.
