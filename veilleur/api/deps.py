"""Dependency-injection factories for the REST API.

Tests override these via ``app.dependency_overrides`` to swap in a
``FakePassePartout`` and a stub LLM client.
"""

from __future__ import annotations

import httpx

from veilleur.config import Settings, get_settings
from veilleur.scraper import PassePartoutClient, Scraper
from veilleur.xpath import HttpxLLMClient, LLMClient, LLMClientError


def _settings() -> Settings:
    return get_settings()


async def get_scraper() -> Scraper:
    """Build the real ``PassePartoutClient`` from process settings.

    The returned client owns its own ``httpx.AsyncClient``; FastAPI keeps
    one instance per request resolution. For now this is acceptable —
    scrapes are serialized and short-lived. The scheduler (Phase 8) will
    construct a long-lived client outside the request lifecycle.
    """
    settings = _settings()
    base_url = settings.PASSEPARTOUT_URL
    if not base_url:
        raise RuntimeError("PASSEPARTOUT_URL is not configured")
    token = (
        settings.PASSEPARTOUT_BEARER_TOKEN.get_secret_value()
        if settings.PASSEPARTOUT_BEARER_TOKEN
        else None
    )
    return PassePartoutClient(base_url=base_url, bearer_token=token)


async def get_llm_client() -> LLMClient:
    """Build the default ``HttpxLLMClient`` from process settings.

    Reads ``LLM_API_URL`` / ``LLM_MODEL_NAME`` / ``LLM_API_KEY`` via
    ``get_settings()`` (consistent with the rest of the app) instead of
    going through ``HttpxLLMClient.from_env``.
    """
    settings = _settings()
    api_url = settings.LLM_API_URL or ""
    model = settings.LLM_MODEL_NAME or ""
    api_key = settings.LLM_API_KEY.get_secret_value() if settings.LLM_API_KEY else ""
    missing = [
        name
        for name, value in (
            ("LLM_API_URL", api_url),
            ("LLM_MODEL_NAME", model),
            ("LLM_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise LLMClientError(f"missing required environment variables: {', '.join(missing)}")
    http = httpx.AsyncClient(timeout=settings.LLM_HTTP_TIMEOUT_SECONDS)
    return HttpxLLMClient(api_url=api_url, model=model, api_key=api_key, http=http)
