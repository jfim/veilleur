"""``/healthz`` endpoint tests against the testcontainer DB."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient


def test_healthz_ok(alembic_upgraded: None) -> None:
    """With the testcontainer up and migrated, /healthz returns 200."""
    # alembic_upgraded fixture sets POSTGRES_* env vars; the FastAPI session
    # dependency builds its engine from get_settings(), which we cleared in
    # the fixture so it picks up the container's connection params.
    from veilleur.app import app

    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "db": "ok"}


def test_healthz_degraded_on_bad_db(monkeypatch: object) -> None:
    """Pointing config at an unreachable DB makes /healthz return 503."""
    # Save & override the engine factory to talk to a port nothing listens on.
    from veilleur.config import get_settings
    from veilleur.db import session as db_session

    old_host = os.environ.get("POSTGRES_HOST")
    old_port = os.environ.get("POSTGRES_PORT")
    os.environ["POSTGRES_HOST"] = "127.0.0.1"
    os.environ["POSTGRES_PORT"] = "1"  # nothing listens here
    get_settings.cache_clear()
    db_session._engine = None  # type: ignore[attr-defined]
    db_session._session_factory = None  # type: ignore[attr-defined]

    try:
        from veilleur.app import app

        with TestClient(app) as client:
            r = client.get("/healthz")
            assert r.status_code == 503
            body = r.json()
            assert body["status"] == "degraded"
            assert isinstance(body["db"], str)
    finally:
        if old_host is None:
            os.environ.pop("POSTGRES_HOST", None)
        else:
            os.environ["POSTGRES_HOST"] = old_host
        if old_port is None:
            os.environ.pop("POSTGRES_PORT", None)
        else:
            os.environ["POSTGRES_PORT"] = old_port
        get_settings.cache_clear()
        db_session._engine = None  # type: ignore[attr-defined]
        db_session._session_factory = None  # type: ignore[attr-defined]
