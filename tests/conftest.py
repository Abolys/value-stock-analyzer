"""Shared test fixtures. Every test runs offline: sockets are blocked."""

from __future__ import annotations

import socket
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.cache import DiskCache  # noqa: E402
from data.fixture_provider import fixture_provider  # noqa: E402

HANDMADE = ROOT / "tests" / "fixtures" / "handmade"


@pytest.fixture(autouse=True)
def _temp_runs_db(monkeypatch, tmp_path):
    """Code that reads config.RUNS_DB_PATH at call time (the app) never touches the real runs.db in tests."""
    import config

    monkeypatch.setattr(config, "RUNS_DB_PATH", tmp_path / "app_runs.db")


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch, request, tmp_path):
    """Offline tests never see a real API key, the Claude Code CLI or the real LLM cache;
    LLM calls go through mocks."""
    import config
    from llm import departure

    monkeypatch.setattr(config, "LLM_CACHE_DB_PATH", tmp_path / "llm_cache.db")
    monkeypatch.setattr(departure, "_default", None)
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "LLM_BACKEND", "api")  # never fall back to the real CLI offline


@pytest.fixture(autouse=True)
def _no_background_checks(monkeypatch):
    """The app launches the alert check as a background process; tests record the launch instead."""
    from app import alert_jobs

    launched: list = []

    def fake_launch(source="app_start", tickers=None, db_path=None):
        launched.append((source, tickers))
        return 0

    monkeypatch.setattr(alert_jobs, "launch", fake_launch)
    return launched


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, request):
    if request.node.get_closest_marker("live"):
        return

    def guard(*_a, **_k):
        raise RuntimeError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 9, 1, 12, 0))


@pytest.fixture
def cache(tmp_path, clock):
    return DiskCache(tmp_path / "cache.db", clock=clock)


@pytest.fixture(scope="session")
def fx_provider():
    """The real YFinanceProvider code path fed from tests/fixtures."""
    return fixture_provider()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "runs.db"
