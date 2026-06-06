"""Milestone 6/7 — thin CLI client (URL resolution only; the loop is I/O)."""

from __future__ import annotations

import pytest

from dexter.cli.repl import _base_url


@pytest.fixture(autouse=True)
def _clear_url_env(monkeypatch):
    for var in ("DEXTER_URL", "DEXTER_HOST", "DEXTER_PORT"):
        monkeypatch.delenv(var, raising=False)


def test_defaults_to_localhost_8000():
    assert _base_url() == "http://127.0.0.1:8000"


def test_host_and_port_from_env(monkeypatch):
    monkeypatch.setenv("DEXTER_HOST", "0.0.0.0")
    monkeypatch.setenv("DEXTER_PORT", "9000")
    assert _base_url() == "http://0.0.0.0:9000"


def test_explicit_url_wins_and_trailing_slash_stripped(monkeypatch):
    monkeypatch.setenv("DEXTER_URL", "https://dexter.example.com/")
    monkeypatch.setenv("DEXTER_HOST", "ignored")
    assert _base_url() == "https://dexter.example.com"
