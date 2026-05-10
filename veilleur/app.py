"""FastAPI application entry point for Veilleur.

Mounts ``/healthz`` (unauthenticated) and the bearer-token-protected REST
API under ``/`` (Phase 6). Public RSS/Atom serving lives in
:mod:`veilleur.feeds`. The background scheduler (Phase 8) is wired in via
the lifespan handler.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from veilleur.api.routes import api_router
from veilleur.config import Settings, get_settings
from veilleur.db.session import get_session, get_session_factory
from veilleur.feeds import feeds_public_router
from veilleur.pipeline import ScrapeOutcome, run_scrape
from veilleur.scheduler import SchedulerLoop
from veilleur.scraper import PassePartoutClient, validate_raw_html_dir
from veilleur.web import STATIC_DIR, STATIC_PATH, web_router
from veilleur.xpath import HttpxLLMClient

logger = logging.getLogger(__name__)


def _validate_startup_config(settings: Settings) -> None:
    """Fail fast when required env vars are missing.

    The REST API, the web UI, and the scrape pipeline each have a hard
    dependency on a small set of values; without them the app would either
    reject every request or surface confusing mid-scrape errors. We raise
    here so the operator sees the problem at boot, not on the first hit.
    """
    problems: list[str] = []
    if settings.API_BEARER_TOKEN is None and not settings.API_AUTH_DISABLED:
        problems.append(
            "API_BEARER_TOKEN is unset (set it, or set API_AUTH_DISABLED=true"
            " for unauthenticated local dev)"
        )
    if not settings.PASSEPARTOUT_URL:
        problems.append("PASSEPARTOUT_URL is unset")
    llm_missing = [
        name
        for name, value in (
            ("LLM_API_URL", settings.LLM_API_URL),
            ("LLM_MODEL_NAME", settings.LLM_MODEL_NAME),
            ("LLM_API_KEY", settings.LLM_API_KEY),
        )
        if not value
    ]
    if llm_missing:
        problems.append(f"LLM config incomplete: missing {', '.join(llm_missing)}")
    if problems:
        raise RuntimeError(
            "veilleur is misconfigured:\n  - " + "\n  - ".join(problems)
        )


def _build_scheduler() -> tuple[SchedulerLoop, list[object]]:
    """Construct a scheduler against the real scraper + LLM clients.

    Required outbound config is validated upstream by
    :func:`_validate_startup_config`, so by the time we reach this function
    the URLs and the LLM credential are guaranteed to be set.
    """
    settings = get_settings()
    pp_token = (
        settings.PASSEPARTOUT_BEARER_TOKEN.get_secret_value()
        if settings.PASSEPARTOUT_BEARER_TOKEN
        else None
    )
    scraper = PassePartoutClient(
        base_url=settings.PASSEPARTOUT_URL or "",
        bearer_token=pp_token,
    )
    llm_http = httpx.AsyncClient(timeout=settings.LLM_HTTP_TIMEOUT_SECONDS)
    llm = HttpxLLMClient(
        api_url=settings.LLM_API_URL or "",
        model=settings.LLM_MODEL_NAME or "",
        api_key=(
            settings.LLM_API_KEY.get_secret_value()
            if settings.LLM_API_KEY
            else ""
        ),
        http=llm_http,
    )

    async def _scrape_one(feed_id: uuid.UUID) -> ScrapeOutcome:
        return await run_scrape(feed_id, scraper=scraper, llm=llm)

    scheduler = SchedulerLoop(
        scrape=_scrape_one,
        session_factory=get_session_factory(),
        tick_seconds=settings.SCHEDULER_TICK_SECONDS,
    )
    # Hand the owned clients back so the lifespan can close them on shutdown.
    return scheduler, [scraper, llm_http]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    _validate_startup_config(settings)
    if settings.RAW_HTML_DIR is not None:
        # Fail fast on a misconfigured directory rather than discovering it
        # mid-scrape and silently dropping HTML payloads.
        validate_raw_html_dir(settings.RAW_HTML_DIR)
    scheduler: SchedulerLoop | None = None
    owned: list[object] = []
    if settings.SCHEDULER_ENABLED:
        scheduler, owned = _build_scheduler()
        scheduler.start()

    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        for resource in owned:
            close = getattr(resource, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # pragma: no cover - best-effort shutdown
                logger.exception("error closing %r during shutdown", resource)


app = FastAPI(title="Veilleur", version="0.1.0", lifespan=lifespan)
# Honour ``X-Forwarded-Proto``/``X-Forwarded-For`` so ``request.url_for`` and
# redirects use the external scheme (https) when behind a TLS-terminating
# proxy. Without this, generated form URLs come back as ``http://``.
app.add_middleware(
    ProxyHeadersMiddleware,
    trusted_hosts=get_settings().FORWARDED_ALLOW_IPS,
)
# Public RSS/Atom routes must be registered *before* the bearer-protected
# router so that overlapping paths (e.g. ``/feeds/{id}/rss``) match the
# unauthenticated handler first.
app.include_router(feeds_public_router)
app.include_router(api_router)
app.include_router(web_router)
app.mount(STATIC_PATH, StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Liveness probe — pings the local Postgres with ``SELECT 1``.

    Does **not** probe passe-partout, the LLM endpoint, or any other dependency.
    """
    try:
        result = await session.execute(text("SELECT 1"))
        result.scalar_one()
    except SQLAlchemyError as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": type(exc).__name__},
        )
    except Exception as exc:  # pragma: no cover - defensive: connection-level failures
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": type(exc).__name__},
        )
    return JSONResponse(status_code=200, content={"status": "ok", "db": "ok"})
