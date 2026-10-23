"""In-memory Broker for tests and local runs.

Tests drive it directly: set quotes, fill orders, inject rejections and warnings, drop
the connection. It fires the same callbacks the real broker would.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime

from ibkr_exec.broker.base import (
    BrokerDisconnected,
    BrokerError,
    DisconnectCallback,
    FillCallback,
    OrderRejected,
    OrderStatusCallback,
    WarningCallback,
)
from ibkr_exec.calendar import ny_date
from ibkr_exec.clock import Clock
from ibkr_exec.domain import (
    AccountState,
    BracketIds,
    BracketRequest,
    BrokerOrder,
    Fill,
    OrderRequest,
    OrderStatus,
    Position,
    Quote,
    Warning,
)


@dataclass
class _Rec:
    order_id: int
    symbol: str
    action: str
    order_type: str
    qty: int
    limit_price: float | None
    stop_price: float | None
    order_ref: str
    parent_id: int | None
    oca_group: str | None
    tif: str
    created_at: datetime
    updated_at: datetime
    status: OrderStatus = OrderStatus.SUBMITTED
    filled: int = 0
    perm_id: int | None = None
    note: str = ""

    def snapshot(self) -> BrokerOrder:
        return BrokerOrder(
            order_id=self.order_id,
            perm_id=self.perm_id,
            symbol=self.symbol,
            action=self.action,
            order_type=self.order_type,
            qty=self.qty,
            filled=self.filled,
            limit_price=self.limit_price,
            stop_price=self.stop_price,
            status=self.status,
            order_ref=self.order_ref,
            parent_id=self.parent_id,
            oca_group=self.oca_group,
            tif=self.tif,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


@dataclass
class _Pos:
    qty: int = 0
    avg_cost: float = 0.0


@dataclass
class FakeBroker:
    clock: Clock
    account_id: str = "DU1234567"
    cash: float = 10_000.0
    commission_per_fill: float = 1.0
    realized_pnl_today: float = 0.0

    _connected: bool = False
    _connected_at: datetime | None = None
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1001))
    _orders: dict[int, _Rec] = field(default_factory=dict)
    _positions: dict[str, _Pos] = field(default_factory=dict)
    _quotes: dict[str, Quote] = field(default_factory=dict)
    _fills: list[Fill] = field(default_factory=list)
    _status_cbs: list[OrderStatusCallback] = field(default_factory=list)
    _fill_cbs: list[FillCallback] = field(default_factory=list)
    _warning_cbs: list[WarningCallback] = field(default_factory=list)
    _disconnect_cbs: list[DisconnectCallback] = field(default_factory=list)

    # test knobs
    reject_next_reason: str | None = None
    fail_next_cancel: bool = False
    fail_next_place: bool = False
    placed: list[BracketRequest | OrderRequest] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    modified: list[tuple[int, int]] = field(default_factory=list)

    # ── session ───────────────────────────────────────────────────────────
    async def connect(self) -> None:
        self._connected = True
        self._connected_at = self.clock.now()

    async def disconnect(self) -> None:
        self._connected = False
        self._connected_at = None

    def is_connected(self) -> bool:
        return self._connected

    def connected_since(self) -> datetime | None:
        return self._connected_at

    async def server_time(self) -> datetime:
        self._require()
        return self.clock.now()

    async def drop_connection(self) -> None:
        """Simulate the gateway going away."""
        await self.disconnect()
        for cb in self._disconnect_cbs:
            await cb()

    def _require(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("fake broker is not connected")

    # ── state ─────────────────────────────────────────────────────────────
    async def account_state(self) -> AccountState:
        self._require()
        mv = 0.0
        unreal = 0.0
        for sym, p in self._positions.items():
            if p.qty == 0:
                continue
            px = self._quotes[sym].last if sym in self._quotes else p.avg_cost
            mv += p.qty * px
            unreal += p.qty * (px - p.avg_cost)
        return AccountState(
            account_id=self.account_id,
            net_liquidation=self.cash + mv,
            available_funds=self.cash,
            settled_cash=self.cash,
            buying_power=self.cash,
            realized_pnl_today=self.realized_pnl_today,
            unrealized_pnl=unreal,
            as_of=self.clock.now(),
        )

    async def positions(self) -> list[Position]:
        self._require()
        return [
            Position(sym, p.qty, p.avg_cost) for sym, p in self._positions.items() if p.qty != 0
        ]

    async def open_orders(self) -> list[BrokerOrder]:
        self._require()
        return [r.snapshot() for r in self._orders.values() if r.status.is_working]

    async def orders_today(self) -> list[BrokerOrder]:
        self._require()
        today = ny_date(self.clock.now())
        return [r.snapshot() for r in self._orders.values() if ny_date(r.created_at) == today]

    async def executions_since(self, since: datetime | None) -> list[Fill]:
        self._require()
        return [f for f in self._fills if since is None or f.time > since]

    async def quote(self, symbol: str) -> Quote:
        self._require()
        q = self._quotes.get(symbol.upper())
        if q is None:
            raise BrokerError(f"no market data for {symbol}")
        return q

    # ── actions ───────────────────────────────────────────────────────────
    async def place_bracket(self, req: BracketRequest) -> BracketIds:
        self._require()
        self._take_rejection()
        now = self.clock.now()
        pid = next(self._ids)
        sid = next(self._ids)
        tid = next(self._ids)
        self._orders[pid] = _Rec(
            pid,
            req.symbol,
            "BUY",
            "LMT",
            req.qty,
            req.limit_price,
            None,
            req.signal_id,
            None,
            None,
            "DAY",
            now,
            now,
        )
        self._orders[sid] = _Rec(
            sid,
            req.symbol,
            "SELL",
            "STP",
            req.qty,
            None,
            req.stop_price,
            req.signal_id,
            pid,
            req.signal_id,
            "GTC",
            now,
            now,
        )
        self._orders[tid] = _Rec(
            tid,
            req.symbol,
            "SELL",
            "LMT",
            req.qty,
            req.target_price,
            None,
            req.signal_id,
            pid,
            req.signal_id,
            "GTC",
            now,
            now,
        )
        self.placed.append(req)
        for oid in (pid, sid, tid):
            await self._emit_status(oid)
        return BracketIds(pid, sid, tid)

    async def place_order(self, req: OrderRequest) -> int:
        self._require()
        self._take_rejection()
        now = self.clock.now()
        oid = next(self._ids)
        self._orders[oid] = _Rec(
            oid,
            req.symbol,
            req.action,
            req.order_type,
            req.qty,
            req.limit_price,
            req.stop_price,
            req.order_ref,
            None,
            None,
            req.tif,
            now,
            now,
        )
        self.placed.append(req)
        await self._emit_status(oid)
        return oid

    async def modify_qty(self, order_id: int, qty: int) -> None:
        self._require()
        rec = self._orders[order_id]
        rec.qty = qty
        rec.updated_at = self.clock.now()
        self.modified.append((order_id, qty))
        await self._emit_status(order_id)

    async def cancel(self, order_id: int) -> None:
        self._require()
        if self.fail_next_cancel:
            self.fail_next_cancel = False
            raise BrokerError(f"cancel of {order_id} failed (injected)")
        rec = self._orders.get(order_id)
        if rec is None:
            raise BrokerError(f"unknown order {order_id}")
        if rec.status.is_terminal:
            return
        rec.status = OrderStatus.CANCELLED
        rec.updated_at = self.clock.now()
        self.cancelled.append(order_id)
        await self._emit_status(order_id)
        # IBKR cancels a parent's unfilled children with it when nothing has filled.
        if rec.filled == 0:
            for child in list(self._orders.values()):
                if child.parent_id == order_id and child.status.is_working:
                    child.status = OrderStatus.CANCELLED
                    child.updated_at = rec.updated_at
                    await self._emit_status(child.order_id)

    def _take_rejection(self) -> None:
        if self.fail_next_place:
            self.fail_next_place = False
            raise BrokerError("placement failed (injected)")
        if self.reject_next_reason:
            reason, self.reject_next_reason = self.reject_next_reason, None
            raise OrderRejected(None, reason)

    # ── events ────────────────────────────────────────────────────────────
    def on_order_status(self, cb: OrderStatusCallback) -> None:
        self._status_cbs.append(cb)

    def on_fill(self, cb: FillCallback) -> None:
        self._fill_cbs.append(cb)

    def on_warning(self, cb: WarningCallback) -> None:
        self._warning_cbs.append(cb)

    def on_disconnect(self, cb: DisconnectCallback) -> None:
        self._disconnect_cbs.append(cb)

    async def _emit_status(self, order_id: int) -> None:
        snap = self._orders[order_id].snapshot()
        for cb in self._status_cbs:
            await cb(snap)

    # ── test controls ─────────────────────────────────────────────────────
    def set_quote(
        self,
        symbol: str,
        last: float,
        bid: float | None = None,
        ask: float | None = None,
        ts: datetime | None = None,
        delayed: bool = False,
    ) -> Quote:
        q = Quote(
            symbol=symbol.upper(),
            bid=bid if bid is not None else round(last - 0.01, 2),
            ask=ask if ask is not None else round(last + 0.01, 2),
            last=last,
            ts=ts or self.clock.now(),
            delayed=delayed,
        )
        self._quotes[q.symbol] = q
        return q

    def set_position(self, symbol: str, qty: int, avg_cost: float) -> None:
        """Put a position on the account directly (e.g. an 'unknown' one for drift tests)."""
        self._positions[symbol.upper()] = _Pos(qty, avg_cost)

    def add_external_order(self, req: OrderRequest, order_ref: str = "") -> int:
        """An order the service did not place (drift tests). Fires no callbacks."""
        now = self.clock.now()
        oid = next(self._ids)
        self._orders[oid] = _Rec(
            oid,
            req.symbol,
            req.action,
            req.order_type,
            req.qty,
            req.limit_price,
            req.stop_price,
            order_ref,
            None,
            None,
            req.tif,
            now,
            now,
        )
        return oid

    def order(self, order_id: int) -> BrokerOrder:
        return self._orders[order_id].snapshot()

    def orders_with_ref(self, order_ref: str) -> list[BrokerOrder]:
        return [r.snapshot() for r in self._orders.values() if r.order_ref == order_ref]

    async def fill(self, order_id: int, qty: int | None = None, price: float | None = None) -> Fill:
        """Fill (part of) an order: updates positions, cash, P&L, OCA siblings; fires callbacks."""
        rec = self._orders[order_id]
        if not rec.status.is_working:
            raise BrokerError(f"order {order_id} is {rec.status}")
        qty = rec.qty - rec.filled if qty is None else min(qty, rec.qty - rec.filled)
        if price is None:
            price = rec.limit_price or rec.stop_price or self._quotes[rec.symbol].last
        now = self.clock.now()
        rec.filled += qty
        rec.updated_at = now
        rec.status = OrderStatus.FILLED if rec.filled >= rec.qty else OrderStatus.PARTIAL

        pos = self._positions.setdefault(rec.symbol, _Pos())
        if rec.action == "BUY":
            total_cost = pos.avg_cost * pos.qty + price * qty
            pos.qty += qty
            pos.avg_cost = total_cost / pos.qty if pos.qty else 0.0
            self.cash -= price * qty
        else:
            self.realized_pnl_today += (price - pos.avg_cost) * qty - self.commission_per_fill
            pos.qty -= qty
            self.cash += price * qty
            if pos.qty == 0:
                pos.avg_cost = 0.0
        self.cash -= self.commission_per_fill

        f = Fill(
            exec_id=f"fake-{order_id}-{len(self._fills) + 1}",
            order_id=order_id,
            symbol=rec.symbol,
            action=rec.action,
            qty=qty,
            price=price,
            commission=self.commission_per_fill,
            time=now,
            order_ref=rec.order_ref,
        )
        self._fills.append(f)
        await self._emit_status(order_id)
        for cb in self._fill_cbs:
            await cb(f)

        # OCA: a fully filled child cancels its siblings.
        if rec.status is OrderStatus.FILLED and rec.oca_group:
            for other in list(self._orders.values()):
                if (
                    other.oca_group == rec.oca_group
                    and other.order_id != rec.order_id
                    and other.status.is_working
                ):
                    other.status = OrderStatus.CANCELLED
                    other.updated_at = now
                    await self._emit_status(other.order_id)
        return f

    async def reject(self, order_id: int, reason: str) -> None:
        """Broker-side rejection after acknowledgement (e.g. a precaution)."""
        rec = self._orders[order_id]
        rec.status = OrderStatus.REJECTED
        rec.note = reason
        rec.updated_at = self.clock.now()
        await self._emit_status(order_id)
        await self.emit_warning(201, f"Order rejected - reason: {reason}", order_id)

    async def emit_warning(self, code: int, message: str, order_id: int | None = None) -> None:
        w = Warning(code=code, message=message, order_id=order_id, at=self.clock.now())
        for cb in self._warning_cbs:
            await cb(w)
