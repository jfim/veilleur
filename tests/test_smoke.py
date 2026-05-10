"""Smoke tests: app imports and config builds."""

from _pytest.monkeypatch import MonkeyPatch

from veilleur.app import app
from veilleur.config import Settings


def test_app_imports() -> None:
    assert app.title == "Veilleur"


def test_settings_defaults(monkeypatch: MonkeyPatch) -> None:
    # Clear any POSTGRES_* env so we can assert on defaults.
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    # Don't read .env from cwd either.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.POSTGRES_DB == "veilleur"
    assert s.LOG_LEVEL == "INFO"
    assert s.XPATH_MAX_ANCHORS == 250
    assert s.database_url_async.startswith("postgresql+asyncpg://")
    assert s.database_url_sync.startswith("postgresql+psycopg://")


def test_healthz_route_registered() -> None:
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/healthz" in routes
