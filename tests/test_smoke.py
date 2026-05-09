"""Smoke test: the FastAPI app imports and exposes /health."""

from fastapi.testclient import TestClient

from veilleur.app import app


def test_app_imports() -> None:
    assert app.title == "Veilleur"


def test_health_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
