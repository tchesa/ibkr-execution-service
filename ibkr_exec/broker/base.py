"""The Broker protocol: everything the service needs from IBKR, and nothing more.

The risk engine, order logic, jobs and tests depend only on this module and on
``domain``. ``ib_async`` is imported solely by ``broker/ib.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from ibkr_exec.domain import (
    AccountState,
    BracketIds,
    BracketRequest,
    BrokerOrder,
    Fill,
    OrderRequest,
    Position,
    Quote,
    Warning,
)

OrderStatusCallback = Callable[[BrokerOrder], Awaitable[None]]
FillCallback = Callable[[Fill], Awaitable[None]]
WarningCallback = Callable[[Warning], Awaitable[None]]
DisconnectCallback = Callable[[], Awaitable[None]]


class BrokerError(Exception):
    """Raised when the broker refuses or cannot complete an operation."""


class BrokerDisconnected(BrokerError):
    pass


class OrderRejected(BrokerError):
    def __init__(self, order_id: int | None, reason: str) -> None:
        super().__init__(reason)
        self.order_id = order_id
        self.reason = reason


class Broker(Protocol):
    # ── session ───────────────────────────────────────────────────────────
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    async def server_time(self) -> datetime: ...
    def connected_since(self) -> datetime | None: ...

    # ── state (always read live) ──────────────────────────────────────────
    async def account_state(self) -> AccountState: ...
    async def positions(self) -> list[Position]: ...
    async def open_orders(self) -> list[BrokerOrder]: ...
    async def orders_today(self) -> list[BrokerOrder]: ...
    async def executions_since(self, since: datetime | None) -> list[Fill]: ...
    async def quote(self, symbol: str) -> Quote: ...

    # ── actions ───────────────────────────────────────────────────────────
    async def place_bracket(self, req: BracketRequest) -> BracketIds: ...
    async def place_order(self, req: OrderRequest) -> int: ...
    async def modify_qty(self, order_id: int, qty: int) -> None: ...
    async def cancel(self, order_id: int) -> None:
        """Cancel and wait for the broker to confirm, or raise BrokerError."""
        ...

    # ── events ────────────────────────────────────────────────────────────
    def on_order_status(self, cb: OrderStatusCallback) -> None: ...
    def on_fill(self, cb: FillCallback) -> None: ...
    def on_warning(self, cb: WarningCallback) -> None: ...
    def on_disconnect(self, cb: DisconnectCallback) -> None: ...
