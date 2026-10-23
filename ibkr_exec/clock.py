"""Injectable clock so every rule and job can be tested at a fixed instant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class Clock(Protocol):
    def now(self) -> datetime:
        """Aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock tests move by hand. ``set``/``advance`` return the new time."""

    def __init__(self, at: datetime) -> None:
        self._now = _to_utc(at)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> datetime:
        self._now = _to_utc(at)
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("clock times must be timezone-aware")
    return dt.astimezone(UTC)


def ny(dt: datetime) -> datetime:
    """Convert an aware datetime to New York local time."""
    return dt.astimezone(NY)


def ny_time(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build an aware New York datetime (handy in tests and schedules)."""
    return datetime(year, month, day, hour, minute, tzinfo=NY)
