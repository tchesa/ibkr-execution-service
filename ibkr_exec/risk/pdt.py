"""Pattern-day-trade counting from recorded fills.

A day trade is a buy and a sell of the same symbol on the same New York date. With the
duplicate rule (one entry per symbol per day) each such pair is one day trade, so the
count is the number of (date, symbol) pairs that saw both sides.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from ibkr_exec.calendar import business_days_back, ny_date
from ibkr_exec.domain import Fill


def count_day_trades(fills: Iterable[Fill]) -> int:
    sides: dict[tuple[date, str], set[str]] = defaultdict(set)
    for f in fills:
        sides[(ny_date(f.time), f.symbol.upper())].add(f.action.upper())
    return sum(1 for s in sides.values() if {"BUY", "SELL"} <= s)


def pdt_window(today: date, business_days: int) -> tuple[date, date]:
    """Inclusive [start, end] of the rolling window ending today."""
    return business_days_back(today, business_days - 1), today
