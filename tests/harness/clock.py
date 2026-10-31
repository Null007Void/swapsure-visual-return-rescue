"""Controllable clock for deterministic time-dependent tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class ManualClock:
    """A clock the test advances explicitly; never reads wall time."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start")
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware value")
        self._now = value
        return self._now

    def utc(self) -> datetime:
        return self._now.astimezone(timezone.utc)
