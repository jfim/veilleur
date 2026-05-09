"""Application configuration loaded from the environment via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for Veilleur."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="VEILLEUR_", extra="ignore")

    database_url: str = "postgresql+psycopg://veilleur:veilleur@localhost:5432/veilleur"
    passe_partout_url: str = "http://localhost:8080"
    anthropic_api_key: str = ""


settings = Settings()
