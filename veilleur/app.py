"""FastAPI application entry point for Veilleur.

Phase 1 only exposes ``/healthz``; route modules will be mounted here as they
land in later phases.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from veilleur.db.session import get_session

app = FastAPI(title="Veilleur", version="0.1.0")


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
