"""FastAPI application entry point for Veilleur.

Wires up the web UI, REST API, and feed-serving routes. Currently a stub
exposing only a health check; route modules will be mounted here as they
are implemented.
"""

from fastapi import FastAPI

app = FastAPI(title="Veilleur", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
