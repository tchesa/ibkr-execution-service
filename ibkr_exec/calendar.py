"""NYSE session helpers: trading days, early closes, entry window, business-day math.

Backed by exchange_calendars (XNYS) for holidays and early closes and by zoneinfo for
DST. Nothing here hand-rolls a time zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from ibkr_exec.clock import NY, ny

_REGULAR_CLOSE = time(16, 0)


@lru_cache(maxsize=1)
def _xnys() -> xcals.ExchangeCalendar:
    # A generous fixed range keeps the calendar deterministic across runs.
    return xcals.get_calendar("XNYS", start="2000-01-01", end="2040-12-31")


def is_trading_day(d: date) -> bool:
    return bool(_xnys().is_session(pd.Timestamp(d)))


def session_close(d: date) -> time | None:
    """Local close time for a session, or None if the market is shut that day."""
    if not is_trading_day(d):
        return None
    close_utc = _xnys().session_close(pd.Timestamp(d))
    return close_utc.tz_convert(NY).time()


def is_early_close(d: date) -> bool:
    c = session_close(d)
    return c is not None and c < _REGULAR_CLOSE


def entry_window_open(
    now: datetime,
    window: tuple[time, time],
    early_close_entry_end: time,
) -> tuple[bool, str]:
    """Whether new entries are allowed at ``now`` (aware), with an explanation."""
    local = ny(now)
    d = local.date()
    if not is_trading_day(d):
        return False, f"{d.isoformat()} is not an NYSE session"
    start, end = window
    if is_early_close(d):
        end = min(end, early_close_entry_end)
    t = local.time().replace(microsecond=0)
    if t < start:
        return False, f"before entry window ({t} < {start} NY)"
    if t >= end:
        why = "early close" if is_early_close(d) else "entry window end"
        return False, f"after {why} ({t} >= {end} NY)"
    return True, f"within entry window ({start}-{end} NY)"


def ny_date(now: datetime) -> date:
    return ny(now).date()


def ny_day_bounds(d: date) -> tuple[datetime, datetime]:
    """UTC start/end of the New York calendar day ``d``."""
    start = datetime.combine(d, time(0, 0), tzinfo=NY)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def business_days_back(d: date, n: int) -> date:
    """The session ``n`` sessions before ``d`` (``n=0`` returns the latest session <= d)."""
    cal = _xnys()
    ts = pd.Timestamp(d)
    if not cal.is_session(ts):
        ts = cal.previous_session(ts)
    for _ in range(n):
        ts = cal.previous_session(ts)
    return ts.date()
