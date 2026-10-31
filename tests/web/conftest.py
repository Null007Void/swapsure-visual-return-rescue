"""Fixtures for the Phase C web tests.

Every test runs against the same in-process WSGI application a browser would hit,
with the suite-wide socket guard from ``tests/conftest.py`` still installed.  The
clock is manual so quote expiry, media TTL and preview deadlines are deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harness.clock import ManualClock
from harness.webclient import Client
from ui.service import ReturnService
from ui.webapp import Application

START = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def web_clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def service(data_dir, web_clock):
    instance = ReturnService(data_dir, clock=web_clock)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def app(service):
    return Application(service)


@pytest.fixture
def client(app):
    return Client(app)
