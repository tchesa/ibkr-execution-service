from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ibkr_exec.broker.fake import FakeBroker
from ibkr_exec.clock import FixedClock, ny_time
from ibkr_exec.config import Config
from ibkr_exec.domain import (
    AccountState,
    Entry,
    EntryType,
    HaltState,
    Quote,
    Side,
    Signal,
)
from ibkr_exec.risk.engine import RiskState

# A plain Friday session, 10:15 New York.
T0 = ny_time(2026, 10, 23, 10, 15)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def cfg() -> Config:
    return Config()


@pytest.fixture
def broker(clock: FixedClock) -> FakeBroker:
    return FakeBroker(clock=clock, cash=10_000.0)


def make_signal(
    *,
    signal_id: str = "orb-TSLA-2026-10-23-1015",
    symbol: str = "TSLA",
    side: Side = Side.BUY,
    entry: float = 100.0,
    stop: float | None = 99.0,
    target: float | None = 102.0,
    risk_usd: float = 50,
    expires_at: datetime | None = None,
    qty: int | None = None,
    entry_type: EntryType = EntryType.LIMIT,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        entry=Entry(entry_type, entry),
        stop=stop,
        target=target,
        risk_usd=risk_usd,
        expires_at=expires_at or (T0 + timedelta(minutes=5)),
        strategy="orb-v1",
        reason="test",
        qty=qty,
    )


def make_account(
    *,
    net_liq: float = 10_000.0,
    available: float | None = None,
    realized: float = 0.0,
    account_id: str = "DU1234567",
    as_of: datetime | None = None,
) -> AccountState:
    return AccountState(
        account_id=account_id,
        net_liquidation=net_liq,
        available_funds=net_liq if available is None else available,
        settled_cash=net_liq if available is None else available,
        buying_power=net_liq if available is None else available,
        realized_pnl_today=realized,
        unrealized_pnl=0.0,
        as_of=as_of or T0,
    )


def make_quote(
    symbol: str = "TSLA", last: float = 100.0, ts: datetime | None = None, delayed: bool = False
) -> Quote:
    return Quote(symbol, last - 0.01, last + 0.01, last, ts or T0, delayed)


def make_state(
    *,
    account: AccountState | None = None,
    positions=(),
    orders_today=(),
    quote: Quote | None | str = "default",
    halt: HaltState | None = None,
    day_trades: int = 0,
) -> RiskState:
    return RiskState(
        account=account or make_account(),
        positions=tuple(positions),
        orders_today=tuple(orders_today),
        quote=make_quote() if quote == "default" else quote,
        halt=halt or HaltState(False),
        day_trades_in_window=day_trades,
    )


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)
