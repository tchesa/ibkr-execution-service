"""Domain types shared by the risk engine, order logic, brokers and API.

Everything here is a plain frozen dataclass or enum: no framework imports, so the
risk engine and its tests stay pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class EntryType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"  # placed as a marketable limit, never a true market order


class OrderRole(StrEnum):
    PARENT = "parent"
    STOP = "stop"
    TARGET = "target"
    FLATTEN = "flatten"
    PROTECTIVE_STOP = "protective_stop"


class OrderStatus(StrEnum):
    PENDING = "pending"  # created locally, not yet acknowledged
    SUBMITTED = "submitted"  # working at the broker (PreSubmitted/Submitted)
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INACTIVE = "inactive"

    @property
    def is_working(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.INACTIVE,
        )


@dataclass(frozen=True)
class Entry:
    type: EntryType
    price: float


@dataclass(frozen=True)
class Signal:
    signal_id: str
    symbol: str
    side: Side
    entry: Entry
    stop: float | None
    target: float | None
    risk_usd: float
    expires_at: datetime  # aware
    strategy: str
    reason: str = ""
    qty: int | None = None  # only meaningful for sells: how much to close (None = all)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    ts: datetime  # aware, when the quote was observed
    delayed: bool = False

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_cost: float


@dataclass(frozen=True)
class BrokerOrder:
    order_id: int
    perm_id: int | None
    symbol: str
    action: str  # BUY | SELL
    order_type: str  # LMT | STP | MKT
    qty: int
    filled: int
    limit_price: float | None
    stop_price: float | None
    status: OrderStatus
    order_ref: str
    parent_id: int | None
    oca_group: str | None
    tif: str
    created_at: datetime
    updated_at: datetime

    @property
    def remaining(self) -> int:
        return max(self.qty - self.filled, 0)


@dataclass(frozen=True)
class Fill:
    exec_id: str
    order_id: int
    symbol: str
    action: str
    qty: int
    price: float
    commission: float
    time: datetime
    order_ref: str = ""


@dataclass(frozen=True)
class AccountState:
    account_id: str
    net_liquidation: float
    available_funds: float
    settled_cash: float
    buying_power: float
    realized_pnl_today: float
    unrealized_pnl: float
    as_of: datetime


@dataclass(frozen=True)
class HaltState:
    halted: bool
    reason: str | None = None
    since: datetime | None = None


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Decision:
    accepted: bool
    qty: int
    reasons: tuple[str, ...]
    rule_results: tuple[RuleResult, ...] = field(default_factory=tuple)
    is_exit: bool = False

    @property
    def rejected(self) -> bool:
        return not self.accepted


@dataclass(frozen=True)
class BracketRequest:
    signal_id: str
    symbol: str
    qty: int
    limit_price: float
    stop_price: float
    target_price: float


@dataclass(frozen=True)
class BracketIds:
    parent_id: int
    stop_id: int
    target_id: int


@dataclass(frozen=True)
class OrderRequest:
    order_ref: str
    symbol: str
    action: str  # BUY | SELL
    order_type: str  # LMT | STP
    qty: int
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "DAY"


@dataclass(frozen=True)
class Warning:
    code: int
    message: str
    order_id: int | None
    at: datetime
