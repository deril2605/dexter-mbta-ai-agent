"""Milestone 0 — config loading and required-var validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dexter.config import Settings

# A complete set of env values used to build a sample `.env` in tests.
_FULL_ENV = """\
MBTA_API_KEY=test-mbta-key
MBTA_BASE_URL=https://example.test/mbta
AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com
AZURE_OPENAI_API_KEY=test-azure-key
AZURE_OPENAI_API_VERSION=2024-06-01
AZURE_OPENAI_DEPLOYMENT_ROUTER=router-mini
DEXTER_HOST=0.0.0.0
DEXTER_PORT=9001
LOG_LEVEL=DEBUG
"""


@pytest.fixture(autouse=True)
def _clear_dexter_env(monkeypatch):
    """Ensure ambient process env never leaks into these tests."""
    for var in (
        "MBTA_API_KEY",
        "MBTA_BASE_URL",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT_ROUTER",
        "DEXTER_HOST",
        "DEXTER_PORT",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_loads_all_values_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_FULL_ENV)

    s = Settings(_env_file=str(env))

    assert s.mbta_api_key == "test-mbta-key"
    assert s.mbta_base_url == "https://example.test/mbta"
    assert s.azure_openai_endpoint == "https://example.openai.azure.com"
    assert s.azure_openai_api_key == "test-azure-key"
    assert s.azure_openai_api_version == "2024-06-01"
    assert s.azure_openai_deployment_router == "router-mini"
    assert s.dexter_host == "0.0.0.0"
    assert s.dexter_port == 9001  # coerced to int
    assert s.log_level == "DEBUG"


def test_defaults_applied_when_optional_vars_absent(tmp_path):
    # Only the required Azure vars present; MBTA/service rely on defaults.
    env = tmp_path / ".env"
    env.write_text(
        "AZURE_OPENAI_ENDPOINT=https://example.openai.azure.com\n"
        "AZURE_OPENAI_API_KEY=test-azure-key\n"
        "AZURE_OPENAI_API_VERSION=2024-06-01\n"
        "AZURE_OPENAI_DEPLOYMENT_ROUTER=router-mini\n"
    )

    s = Settings(_env_file=str(env))

    assert s.mbta_api_key is None  # no key -> library still works, just rate-limited
    assert s.mbta_base_url == "https://api-v3.mbta.com"
    assert s.dexter_host == "127.0.0.1"
    assert s.dexter_port == 8000
    assert s.log_level == "INFO"


def test_missing_required_azure_vars_raise_clear_error():
    # No .env, no ambient env (cleared by fixture) -> required Azure vars missing.
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    message = str(excinfo.value)
    assert "azure_openai_endpoint" in message
    assert "azure_openai_api_key" in message
    assert "azure_openai_api_version" in message
    assert "azure_openai_deployment_router" in message
