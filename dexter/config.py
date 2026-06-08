"""Configuration for Dexter, loaded from environment / `.env`.

Design notes
------------
- Azure OpenAI credentials are **required**: a missing one raises a clear
  ``ValidationError`` at load time.
- MBTA values are optional/defaulted — the MBTA core library works without an
  API key (just a lower rate limit). This keeps the LLM-free library usable and
  testable without any Azure credentials present.
- Settings are exposed through the lazy, cached :func:`get_settings` rather than
  a module-global instantiated at import time. Importing this module therefore
  never triggers validation, so the LLM-free library can be imported and tested
  in isolation. The agent/service layers call :func:`get_settings` and pass the
  relevant values down (e.g. into the MBTA client constructor).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All Dexter configuration, sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- MBTA (optional / defaulted) ---
    mbta_api_key: str | None = None
    mbta_base_url: str = "https://api-v3.mbta.com"

    # --- Azure OpenAI (required for the agent) ---
    azure_openai_endpoint: str
    azure_openai_api_key: str
    azure_openai_api_version: str
    azure_openai_deployment_router: str

    # --- Service (defaulted) ---
    dexter_host: str = "127.0.0.1"
    dexter_port: int = 8000
    log_level: str = "INFO"

    # --- Beta web client (optional, off by default) ---
    # When ``dexter_serve_web`` is on, the brain serves the static terminal UI
    # (``web/index.html``) at ``GET /``. ``dexter_passcode`` gates ``/chat``: when
    # set, requests must carry a matching ``X-Dexter-Passcode`` header. Both are
    # off/unset by default so the Phase 1 app and tests are unaffected.
    dexter_serve_web: bool = False
    dexter_passcode: str | None = None
    dexter_deployed_at: str | None = None

    # --- Observability (optional, off by default) ---
    dexter_tracing: bool = False
    dexter_tracing_endpoint: str | None = None  # Phoenix OTLP endpoint; None = default
    dexter_tracing_api_key: str | None = None  # Phoenix Cloud OTLP auth; None = local/none


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, instantiated once.

    Cached so the `.env` is read a single time. Tests that need a fresh load can
    call ``get_settings.cache_clear()`` or instantiate :class:`Settings`
    directly.
    """
    return Settings()
