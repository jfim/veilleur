"""Application configuration loaded from the environment via pydantic-settings.

Env vars are the source of truth; `.env` is read only as a local-dev convenience
and is gitignored. See ``docs/design/phase-1-foundations.md`` §7 for the full
table of variables.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Veilleur."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Outbound LLM (consumed in later phases; optional in Phase 1)
    LLM_API_URL: str | None = None
    LLM_MODEL_NAME: str | None = None
    LLM_API_KEY: SecretStr | None = None

    # Outbound passe-partout (consumed in later phases; optional in Phase 1)
    PASSEPARTOUT_URL: str | None = None
    PASSEPARTOUT_BEARER_TOKEN: SecretStr | None = None

    # Postgres connection
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "veilleur"
    POSTGRES_PASSWORD: SecretStr = SecretStr("veilleur")
    POSTGRES_DB: str = "veilleur"

    # General
    LOG_LEVEL: str = "INFO"

    # Inbound API auth. Startup fails when API_BEARER_TOKEN is unset unless
    # API_AUTH_DISABLED is true (intended for local dev — the REST API and
    # web UI then accept all requests with no credential check).
    API_BEARER_TOKEN: SecretStr | None = None
    API_AUTH_DISABLED: bool = False

    # Phase 3 — declared here for parity, used later
    XPATH_MAX_ANCHORS: int = 250

    # Optional override path for the xpath-derivation prompt template.
    # File absence == use bundled default. See `veilleur.xpath.prompt`.
    PROMPT_FILE: Path | None = None

    # Maximum LLM turns when deriving an xpath. Each turn the model proposes
    # an xpath plus its intended id set; we run it and feed back the diff.
    # The loop stops as soon as intended == actual, the model says ``unable``,
    # or this cap is reached.
    XPATH_MAX_ATTEMPTS: int = 3

    # Scrape defaults (consumed later)
    SCRAPE_DEFAULT_INTERVAL_SECONDS: int = 3600
    SCRAPE_HTTP_TIMEOUT_SECONDS: int = 60

    # LLM HTTP timeout — separate from scrape because thinking-style models
    # routinely take minutes to return.
    LLM_HTTP_TIMEOUT_SECONDS: int = 300

    # Phase 8 — scheduler
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TICK_SECONDS: float = 30.0

    # Filesystem storage for raw fetched HTML.
    # When unset (the default), raw HTML is not persisted anywhere.
    # When set, must be an absolute, writable directory; the scraper writes
    # ``{raw_html_dir}/{feed_uuid}/{year}/{month}/{ts}-{run_short}.html.gz``.
    RAW_HTML_DIR: Path | None = None

    @property
    def database_url_async(self) -> str:
        pw = self.POSTGRES_PASSWORD.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{pw}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def database_url_sync(self) -> str:
        pw = self.POSTGRES_PASSWORD.get_secret_value()
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{pw}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    return Settings()
