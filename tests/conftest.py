"""Shared fixtures: zero-network enforcement, controllable clock, fixture data."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from adapters.mock.fixtures import CatalogFixture, load_catalog_fixture
from harness.clock import ManualClock
from harness.netblock import NetworkGuard


@pytest.fixture(autouse=True)
def no_outbound_network():
    """Block and count outbound sockets for every test in the suite.

    A test that reaches the network fails here even if its own assertions pass:
    the offline build has no authorized egress of any kind.
    """

    guard = NetworkGuard()
    with guard:
        yield guard
    assert guard.attempts == [], (
        "outbound network attempted during the offline suite: " + ", ".join(guard.attempts)
    )


@pytest.fixture
def fixture_clock() -> ManualClock:
    return ManualClock(load_catalog_fixture().clock.astimezone(timezone.utc))


@pytest.fixture
def catalog() -> CatalogFixture:
    return load_catalog_fixture()


@pytest.fixture
def utc() -> type[datetime]:
    return datetime


@pytest.fixture
def store_dir(tmp_path):
    return tmp_path
