"""Environment-backed application configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from ``GRIDBRIEF_`` environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GRIDBRIEF_",
        extra="ignore",
    )

    database_url: PostgresDsn = "postgresql+psycopg://user:password@localhost:5432/gridbrief"
    iso: str = "ERCOT"
    timezone: str = "America/Chicago"
    freshness_minutes: int = Field(default=60, ge=1)
    breaking_price_threshold: float = Field(default=1_000.0, gt=0)
    breaking_outage_threshold_mw: float = Field(default=1_000.0, gt=0)
    require_fresh_data: bool = True
    automatic_refresh: bool = True
    allow_remote_llm: bool = False
    public_mode: bool = False
    contact_email: str = "you@example.com"
    retrieval_backend: Literal["pgvector", "lexical", "chroma"] = "pgvector"
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    chroma_path: Path = Path("./chroma_data")
    ollama_base_url: str = "http://localhost:11434"
    groq_model: str = "<currently-supported-model>"
    llm_cache_path: Path = Path("build/llm_cache.json")
    timeseries_retention_days: int = Field(default=30, ge=8, le=365)
    raw_item_retention_days: int = Field(default=14, ge=1, le=90)
    document_retention_days: int = Field(default=30, ge=8, le=365)
    operations_retention_days: int = Field(default=30, ge=8, le=365)
    edition_retention_days: int = Field(default=90, ge=30, le=730)

    admin_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    ercot_subscription_key: SecretStr | None = None
    ercot_api_username: SecretStr | None = None
    ercot_api_password: SecretStr | None = None
    noaa_cdo_token: SecretStr | None = None
    epa_cam_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Return the cached runtime settings instance."""

    return Settings()
