"""FastAPI application entry point for Veilleur.

Mounts ``/healthz`` (unauthenticated) and the bearer-token-protected REST
API under ``/`` (Phase 6). RSS/Atom and Web UI routes will land in later
phases.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from veilleur.api.routes import api_router
from veilleur.db.session import get_session
from veilleur.feeds import feeds_public_router

app = FastAPI(title="Veilleur", version="0.1.0")
# Public RSS/Atom routes must be registered *before* the bearer-protected
# router so that overlapping paths (e.g. ``/feeds/{id}/rss``) match the
# unauthenticated handler first.
app.include_router(feeds_public_router)
app.include_router(api_router)


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
