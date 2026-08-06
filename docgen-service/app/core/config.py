"""Application settings.

Every setting is overridable via environment variable (or a .env file at the
backend root). `.env.example` at the repo root documents the full set.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- core ---
    app_name: str = "CAD Workbench"
    debug: bool = False
    database_url: str = f"sqlite:///{(BACKEND_ROOT / 'data' / 'app.db').as_posix()}"
    data_dir: Path = BACKEND_ROOT / "data"
    frontend_dist: Path = BACKEND_ROOT.parent / "frontend" / "dist" / "cad-workbench" / "browser"

    # --- sessions / cookies ---
    cookie_secure: bool = True
    session_idle_minutes: int = 480
    session_absolute_hours: int = 24

    # --- auth providers ---
    auth_providers: str = "local"  # comma-separated: local,oidc
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_display_name: str = "Corporate SSO"
    # secret for signing short-lived OIDC state; auto-generated if empty (dev only)
    secret_key: str = ""

    # --- uploads ---
    max_upload_mb: int = 50

    # --- jobs ---
    job_workers: int = 8
    job_buffer_ttl_minutes: int = 30

    # --- LLM ---
    llm_provider: str = "openai_compat"  # openai_compat | azure_openai | bedrock
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_max_concurrency: int = 12
    # Azure specifics (used when llm_provider=azure_openai)
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-10-21"
    # Bedrock specifics (used when llm_provider=bedrock)
    aws_region: str = "us-east-1"

    # model roles (defaults target OpenRouter slugs; remap per deployment)
    llm_model_extraction: str = "google/gemini-2.5-pro"
    llm_model_vision: str = "anthropic/claude-sonnet-4.6"
    llm_model_selection: str = "google/gemini-2.5-pro"
    llm_model_fill: str = "google/gemini-2.5-pro"
    llm_model_analysis: str = "google/gemini-2.5-pro"
    llm_model_chat: str = "google/gemini-2.5-pro"
    llm_model_embedding: str = "openai/text-embedding-3-large"

    @property
    def auth_provider_list(self) -> list[str]:
        return [p.strip() for p in self.auth_providers.split(",") if p.strip()]

    @property
    def oidc_enabled(self) -> bool:
        return "oidc" in self.auth_provider_list and bool(
            self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret
        )


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
