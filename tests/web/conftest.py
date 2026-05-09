"""Re-export the API conftest fixtures so the web UI tests share them.

The ``api_app`` / ``api_factory`` / ``configure_api_token`` / ``stub_llm`` /
``fake_scraper`` fixtures live in ``tests/api/conftest.py`` because they
were introduced for the Phase 6 REST API tests. The web UI sits on the
same FastAPI app, so we re-import the fixtures here rather than
duplicating them.
"""

from __future__ import annotations

from tests.api.conftest import (
    api_app,
    api_client,
    api_factory,
    configure_api_token,
    fake_scraper,
    stub_llm,
)

__all__ = [
    "api_app",
    "api_client",
    "api_factory",
    "configure_api_token",
    "fake_scraper",
    "stub_llm",
]
