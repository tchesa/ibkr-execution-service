"""Broker implementation over the IB Gateway socket API via ib_async.

This is the only module that imports ib_async. It maps gateway objects onto the domain
types, waits for acknowledgements so rejections surface as exceptions, and turns
gateway events into the Broker callbacks.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

from ib_async import (
    IB,
    ExecutionFilter,
    LimitOrder,
    Order,
    Stock,
    StopOrder,
    Trade,
)
from ib_async import Fill as IBFill

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
from ibkr_exec.logging import get_logger

log = get_logger(__name__)

_STATUS = {
    "PendingSubmit": OrderStatus.PENDING,
    "ApiPending": OrderStatus.PENDING,
    "PendingCancel": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "ApiUpdate": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.INACTIVE,
}
_REJECT_CODES = {201, 202, 203, 10147, 10148}  # order rejected / cancelled by the system
_PACING_CODES = {100, 162, 420}
_CONN_LOST = {1100, 2110}
_CONN_RESTORED = {1101, 1102}


def _num(x: float | None) -> float:
    return 0.0 if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _aware(dt: datetime | None, fallback: datetime) -> datetime:
    if dt is None:
        return fallback
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class IBBroker:
    def __init__(self, host: str, port: int, client_id: int, clock: Clock) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._clock = clock
        self._ib = IB()
        self._account = ""
        self._connected_at: datetime | None = None
        self._pnl: Any = None
        self._contracts: dict[str, Stock] = {}
        self._quote_cache: dict[str, tuple[float, Quote]] = {}
        self._last_error: dict[int, tuple[int, str]] = {}
        self._backoff_until: float = 0.0
        self._rejected: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._status_cbs: list[OrderStatusCallback] = []
        self._fill_cbs: list[FillCallback] = []
        self._warning_cbs: list[WarningCallback] = []
        self._disconnect_cbs: list[DisconnectCallback] = []
        self._ib.orderStatusEvent += self._ev_order_status
        self._ib.execDetailsEvent += self._ev_exec
        self._ib.errorEvent += self._ev_error
        self._ib.disconnectedEvent += self._ev_disconnected

    # ── session ───────────────────────────────────────────────────────────
    async def connect(self) -> None:
        if self._ib.isConnected():
            return
        try:
            await self._ib.connectAsync(
                self._host, self._port, clientId=self._client_id, timeout=30, readonly=False
            )
        except Exception as exc:  # noqa: BLE001  (ib_async raises several types)
            raise BrokerDisconnected(f"connect to {self._host}:{self._port} failed: {exc}") from exc
        accounts = self._ib.managedAccounts()
        if not accounts:
            self._ib.disconnect()
            raise BrokerDisconnected("gateway reported no managed accounts")
        self._account = accounts[0]
        self._ib.reqMarketDataType(1)  # live only; delayed data is reported, never used
        try:
            self._pnl = self._ib.reqPnL(self._account)
        except Exception as exc:  # noqa: BLE001
            log.warning("reqPnL_failed", error=str(exc))
            self._pnl = None
        self._connected_at = self._clock.now()
        log.info(
            "ib_connected",
            host=self._host,
            port=self._port,
            account=self._account,
            accounts=accounts,
        )

    async def disconnect(self) -> None:
        self._ib.disconnect()
        self._connected_at = None

    def is_connected(self) -> bool:
        return self._ib.isConnected()

    def connected_since(self) -> datetime | None:
        return self._connected_at

    async def server_time(self) -> datetime:
        self._require()
        t = await self._ib.reqCurrentTimeAsync()
        return _aware(t, self._clock.now())

    def _require(self) -> None:
        if not self._ib.isConnected():
            raise BrokerDisconnected("gateway not connected")

    async def _pace(self) -> None:
        wait = self._backoff_until - asyncio.get_running_loop().time()
        if wait > 0:
            log.warning("pacing_backoff", seconds=round(wait, 1))
            await asyncio.sleep(wait)

    # ── state ─────────────────────────────────────────────────────────────
    async def account_state(self) -> AccountState:
        self._require()
        await self._pace()
        vals = await self._ib.accountSummaryAsync(self._account)
        tags: dict[str, float] = {}
        for v in vals:
            if v.currency in ("USD", "BASE", "") and v.tag in (
                "NetLiquidation",
                "AvailableFunds",
                "SettledCash",
                "BuyingPower",
            ):
                try:
                    tags[v.tag] = float(v.value)
                except ValueError:
                    pass
        realized = unreal = 0.0
        if self._pnl is not None:
            realized = _num(getattr(self._pnl, "realizedPnL", None))
            unreal = _num(getattr(self._pnl, "unrealizedPnL", None))
        return AccountState(
            account_id=self._account,
            net_liquidation=tags.get("NetLiquidation", 0.0),
            available_funds=tags.get("AvailableFunds", 0.0),
            settled_cash=tags.get("SettledCash", tags.get("AvailableFunds", 0.0)),
            buying_power=tags.get("BuyingPower", tags.get("AvailableFunds", 0.0)),
            realized_pnl_today=realized,
            unrealized_pnl=unreal,
            as_of=self._clock.now(),
        )

    async def positions(self) -> list[Position]:
        self._require()
        out = []
        for p in await self._ib.reqPositionsAsync():
            if p.account != self._account or p.contract.secType != "STK":
                continue
            if p.position == 0:
                continue
            out.append(Position(p.contract.symbol, int(p.position), _num(p.avgCost)))
        return out

    async def open_orders(self) -> list[BrokerOrder]:
        self._require()
        return [self._map_trade(t) for t in self._ib.openTrades()]

    async def orders_today(self) -> list[BrokerOrder]:
        self._require()
        today = ny_date(self._clock.now())
        out = []
        for t in self._ib.trades():
            o = self._map_trade(t)
            if ny_date(o.created_at) == today:
                out.append(o)
        return out

    async def executions_since(self, since: datetime | None) -> list[Fill]:
        self._require()
        await self._pace()
        fills = await self._ib.reqExecutionsAsync(ExecutionFilter(clientId=0))
        out = []
        for f in fills:
            mapped = self._map_fill(f)
            if since is None or mapped.time > since:
                out.append(mapped)
        return out

    async def quote(self, symbol: str) -> Quote:
        self._require()
        now_mono = asyncio.get_running_loop().time()
        cached = self._quote_cache.get(symbol.upper())
        if cached and now_mono - cached[0] < 1.0:
            return cached[1]
        await self._pace()
        contract = await self._contract(symbol)
        t = await self._snapshot(contract, symbol)
        delayed = int(getattr(t, "marketDataType", 1) or 1) in (3, 4)
        if _num(t.last) <= 0 and _num(t.bid) <= 0 and _num(t.ask) <= 0:
            # No live entitlement for this symbol (IBKR 354): fall back to delayed data so the
            # caller sees a quote marked delayed, which every rule treats as unusable.
            self._ib.reqMarketDataType(3)
            try:
                t = await self._snapshot(contract, symbol)
            finally:
                self._ib.reqMarketDataType(1)
            delayed = True
        last = _num(t.last) or _num(t.close)
        bid, ask = _num(t.bid), _num(t.ask)
        if last <= 0 and bid <= 0 and ask <= 0:
            raise BrokerError(f"empty market data for {symbol} (live and delayed)")
        q = Quote(
            symbol=symbol.upper(),
            bid=bid,
            ask=ask,
            last=last if last > 0 else (bid + ask) / 2,
            ts=_aware(t.time, self._clock.now()),
            delayed=delayed,
        )
        self._quote_cache[q.symbol] = (now_mono, q)
        return q

    async def _snapshot(self, contract: Stock, symbol: str) -> Any:
        try:
            tickers = await asyncio.wait_for(self._ib.reqTickersAsync(contract), timeout=10)
        except TimeoutError as exc:
            raise BrokerError(f"no market data for {symbol} within 10s") from exc
        if not tickers:
            raise BrokerError(f"no market data for {symbol}")
        return tickers[0]

    async def _contract(self, symbol: str) -> Stock:
        sym = symbol.upper()
        if sym not in self._contracts:
            c = Stock(sym, "SMART", "USD")
            res = await self._ib.qualifyContractsAsync(c)
            if not res or res[0] is None:
                raise BrokerError(f"unknown US stock symbol {sym}")
            self._contracts[sym] = c
        return self._contracts[sym]

    # ── actions ───────────────────────────────────────────────────────────
    async def place_bracket(self, req: BracketRequest) -> BracketIds:
        self._require()
        await self._pace()
        contract = await self._contract(req.symbol)
        bracket = self._ib.bracketOrder(
            "BUY",
            req.qty,
            req.limit_price,
            req.target_price,
            req.stop_price,
            orderRef=req.signal_id,
            outsideRth=False,
        )
        bracket.parent.tif = "DAY"
        for child in (bracket.takeProfit, bracket.stopLoss):
            child.tif = "GTC"
            child.ocaGroup = req.signal_id
            child.ocaType = 1
        trades = [self._ib.placeOrder(contract, o) for o in bracket]
        await self._ack(trades, "bracket")
        return BracketIds(
            parent_id=bracket.parent.orderId,
            stop_id=bracket.stopLoss.orderId,
            target_id=bracket.takeProfit.orderId,
        )

    async def place_order(self, req: OrderRequest) -> int:
        self._require()
        await self._pace()
        contract = await self._contract(req.symbol)
        if req.order_type == "LMT":
            assert req.limit_price is not None
            order: Order = LimitOrder(req.action, req.qty, req.limit_price)
        elif req.order_type == "STP":
            assert req.stop_price is not None
            order = StopOrder(req.action, req.qty, req.stop_price)
        else:
            raise BrokerError(f"unsupported order type {req.order_type}")
        order.tif = req.tif
        order.orderRef = req.order_ref
        order.outsideRth = False
        trade = self._ib.placeOrder(contract, order)
        await self._ack([trade], req.order_type)
        return trade.order.orderId

    async def modify_qty(self, order_id: int, qty: int) -> None:
        self._require()
        trade = self._trade(order_id)
        trade.order.totalQuantity = qty
        self._ib.placeOrder(trade.contract, trade.order)

    async def cancel(self, order_id: int) -> None:
        self._require()
        trade = self._trade(order_id)
        if trade.orderStatus.status in trade.orderStatus.DoneStates:
            return
        self._ib.cancelOrder(trade.order)
        deadline = asyncio.get_running_loop().time() + 10
        while trade.orderStatus.status not in trade.orderStatus.DoneStates:
            if asyncio.get_running_loop().time() > deadline:
                raise BrokerError(f"cancel of {order_id} not confirmed within 10s")
            await asyncio.sleep(0.1)

    def _trade(self, order_id: int) -> Trade:
        for t in self._ib.trades():
            if t.order.orderId == order_id:
                return t
        raise BrokerError(f"unknown order id {order_id}")

    async def _ack(self, trades: list[Trade], what: str) -> None:
        """Wait until every order is acknowledged; raise if any is rejected."""
        deadline = asyncio.get_running_loop().time() + 10
        pending = list(trades)
        while pending:
            still = []
            for t in pending:
                oid = t.order.orderId
                st = t.orderStatus.status
                if oid in self._rejected or st == "Inactive":
                    code, msg = self._last_error.get(oid, (0, "rejected"))
                    for other in trades:
                        if other.orderStatus.status not in other.orderStatus.DoneStates:
                            self._ib.cancelOrder(other.order)
                    raise OrderRejected(oid, f"{what} rejected ({code}): {msg}")
                if st in ("PreSubmitted", "Submitted", "Filled") or st in t.orderStatus.DoneStates:
                    continue
                still.append(t)
            pending = still
            if pending:
                if asyncio.get_running_loop().time() > deadline:
                    ids = [t.order.orderId for t in pending]
                    raise BrokerError(f"{what} not acknowledged within 10s: {ids}")
                await asyncio.sleep(0.05)

    # ── mapping ───────────────────────────────────────────────────────────
    def _map_trade(self, t: Trade) -> BrokerOrder:
        o, s = t.order, t.orderStatus
        now = self._clock.now()
        created = _aware(t.log[0].time, now) if t.log else now
        updated = _aware(t.log[-1].time, now) if t.log else now
        status = _STATUS.get(s.status, OrderStatus.PENDING)
        filled = int(_num(s.filled))
        if status is OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIAL
        if o.orderId in self._rejected or (
            status is OrderStatus.INACTIVE and o.orderId in self._last_error
        ):
            status = OrderStatus.REJECTED
        return BrokerOrder(
            order_id=o.orderId,
            perm_id=o.permId or None,
            symbol=t.contract.symbol,
            action=o.action,
            order_type=o.orderType,
            qty=int(_num(o.totalQuantity)),
            filled=filled,
            limit_price=_num(o.lmtPrice) if o.orderType in ("LMT", "STP LMT") else None,
            stop_price=_num(o.auxPrice) if o.orderType in ("STP", "STP LMT") else None,
            status=status,
            order_ref=o.orderRef or "",
            parent_id=o.parentId or None,
            oca_group=o.ocaGroup or None,
            tif=o.tif or "DAY",
            created_at=created,
            updated_at=updated,
        )

    def _map_fill(self, f: IBFill) -> Fill:
        e = f.execution
        side = "BUY" if e.side.upper().startswith("B") else "SELL"
        return Fill(
            exec_id=e.execId,
            order_id=e.orderId,
            symbol=f.contract.symbol,
            action=side,
            qty=int(_num(e.shares)),
            price=_num(e.price),
            commission=_num(getattr(f.commissionReport, "commission", None)),
            time=_aware(f.time or e.time, self._clock.now()),
            order_ref=e.orderRef or "",
        )

    # ── events ────────────────────────────────────────────────────────────
    def on_order_status(self, cb: OrderStatusCallback) -> None:
        self._status_cbs.append(cb)

    def on_fill(self, cb: FillCallback) -> None:
        self._fill_cbs.append(cb)

    def on_warning(self, cb: WarningCallback) -> None:
        self._warning_cbs.append(cb)

    def on_disconnect(self, cb: DisconnectCallback) -> None:
        self._disconnect_cbs.append(cb)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ev_order_status(self, trade: Trade) -> None:
        o = self._map_trade(trade)
        for cb in self._status_cbs:
            self._spawn(cb(o))

    def _ev_exec(self, trade: Trade, fill: IBFill) -> None:
        f = self._map_fill(fill)
        for cb in self._fill_cbs:
            self._spawn(cb(f))

    def _ev_error(self, reqId: int, errorCode: int, errorString: str, contract: Any) -> None:
        known_ids = {t.order.orderId for t in self._ib.trades()}
        order_id = reqId if reqId in known_ids else None
        if order_id is not None:
            self._last_error[order_id] = (errorCode, errorString)
            if errorCode in _REJECT_CODES:
                self._rejected.add(order_id)
        if errorCode in _PACING_CODES:
            self._backoff_until = asyncio.get_running_loop().time() + 15
            log.warning("ib_pacing", code=errorCode, message=errorString)
        if errorCode in _CONN_LOST:
            log.warning("ib_connectivity_lost", code=errorCode, message=errorString)
            for cb in self._disconnect_cbs:
                self._spawn(cb())
        if errorCode in _CONN_RESTORED:
            log.info("ib_connectivity_restored", code=errorCode)
        level = "info" if errorCode >= 2100 else "warning"
        getattr(log, level)("ib_message", code=errorCode, message=errorString, req_id=reqId)
        w = Warning(code=errorCode, message=errorString, order_id=order_id, at=self._clock.now())
        for cb in self._warning_cbs:
            self._spawn(cb(w))

    def _ev_disconnected(self) -> None:
        self._connected_at = None
        log.warning("ib_disconnected")
        for cb in self._disconnect_cbs:
            self._spawn(cb())


__all__ = ["IBBroker"]
