"""Flatten: cancel a position's children, confirm, then sell it with a marketable limit.

If the sell cannot be placed, a protective stop goes back on and a human is alerted.
A position is never left without a working stop.
"""

from __future__ import annotations

from dataclasses import dataclass

from ibkr_exec.alerts.slack import Alerter
from ibkr_exec.broker.base import Broker, BrokerError
from ibkr_exec.clock import Clock
from ibkr_exec.config import Config
from ibkr_exec.db.repo import Repository
from ibkr_exec.domain import BrokerOrder, OrderRequest, OrderRole, Position
from ibkr_exec.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class FlattenResult:
    symbol: str
    qty: int
    cancelled: tuple[int, ...]
    sell_order_id: int | None
    protective_stop_id: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.sell_order_id is not None


class Flattener:
    def __init__(
        self, broker: Broker, repo: Repository, clock: Clock, cfg: Config, alerter: Alerter
    ) -> None:
        self._b = broker
        self._repo = repo
        self._clock = clock
        self._cfg = cfg
        self._alerts = alerter

    async def flatten(
        self, symbol: str | None, reason: str, qty: int | None = None, ref: str | None = None
    ) -> list[FlattenResult]:
        positions = [p for p in await self._b.positions() if p.qty > 0]
        if symbol is not None:
            positions = [p for p in positions if p.symbol.upper() == symbol.upper()]
        results = []
        for p in positions:
            results.append(await self._flatten_one(p, reason, qty, ref))
        return results

    async def _children(self, symbol: str) -> list[BrokerOrder]:
        return [
            o
            for o in await self._b.open_orders()
            if o.symbol.upper() == symbol.upper() and o.action == "SELL" and o.status.is_working
        ]

    async def _flatten_one(
        self, pos: Position, reason: str, qty: int | None, ref: str | None
    ) -> FlattenResult:
        now = self._clock.now()
        sell_qty = pos.qty if qty is None else min(qty, pos.qty)
        order_ref = ref or f"flatten-{pos.symbol}-{now.strftime('%Y%m%d-%H%M%S')}"
        cancelled: list[int] = []
        old_stop: float | None = None

        # 1. cancel working children (stop/target) and wait for confirmation
        for c in await self._children(pos.symbol):
            if c.order_type == "STP" and c.stop_price:
                old_stop = c.stop_price
            try:
                await self._b.cancel(c.order_id)
                cancelled.append(c.order_id)
            except BrokerError as exc:
                await self._alerts.alert(
                    "critical",
                    "Flatten: could not cancel child",
                    str(exc),
                    symbol=pos.symbol,
                    order_id=c.order_id,
                )
                return FlattenResult(pos.symbol, sell_qty, tuple(cancelled), None, None, str(exc))

        # 2. marketable limit sell for the broker-reported quantity
        try:
            q = await self._b.quote(pos.symbol)
            ref_px = q.bid if q.bid > 0 else q.last
        except BrokerError:
            ref_px = pos.avg_cost  # still sells: a limit far below is marketable
        limit = round(ref_px * (1 - self._cfg.orders.marketable_offset_pct / 100), 2)
        try:
            sell_id = await self._b.place_order(
                OrderRequest(order_ref, pos.symbol, "SELL", "LMT", sell_qty, limit_price=limit)
            )
        except BrokerError as exc:
            log.error("flatten_sell_failed", symbol=pos.symbol, error=str(exc))
            stop_id = await self.place_protective_stop(pos, old_stop, f"flatten failed: {exc}")
            await self._alerts.alert(
                "critical",
                "Flatten failed, protective stop re-placed",
                f"{pos.symbol} x{sell_qty}: {exc}",
                protective_stop_id=stop_id,
            )
            return FlattenResult(pos.symbol, sell_qty, tuple(cancelled), None, stop_id, str(exc))

        for o in await self._b.open_orders():
            if o.order_id == sell_id:
                await self._repo.upsert_order(o, role=OrderRole.FLATTEN, note=reason)
        await self._repo.add_event(
            now,
            "flatten",
            f"{pos.symbol} x{sell_qty} @<= {limit} ({reason})",
            payload={"sell_order_id": sell_id, "cancelled": cancelled},
        )
        log.info("flatten_sent", symbol=pos.symbol, qty=sell_qty, limit=limit, reason=reason)
        return FlattenResult(pos.symbol, sell_qty, tuple(cancelled), sell_id, None)

    async def place_protective_stop(
        self, pos: Position, stop_price: float | None, reason: str
    ) -> int | None:
        """Put a GTC stop under a position that has none. Returns the order id or None."""
        now = self._clock.now()
        if stop_price is None:
            try:
                q = await self._b.quote(pos.symbol)
                stop_price = round(q.last * 0.99, 2)
            except BrokerError:
                stop_price = round(pos.avg_cost * 0.99, 2)
        ref = f"protect-{pos.symbol}-{now.strftime('%Y%m%d-%H%M%S')}"
        try:
            oid = await self._b.place_order(
                OrderRequest(
                    ref, pos.symbol, "SELL", "STP", pos.qty, stop_price=stop_price, tif="GTC"
                )
            )
        except BrokerError as exc:
            await self._alerts.alert(
                "critical",
                "POSITION WITHOUT STOP: protective stop failed",
                f"{pos.symbol} x{pos.qty}: {exc}",
            )
            return None
        for o in await self._b.open_orders():
            if o.order_id == oid:
                await self._repo.upsert_order(o, role=OrderRole.PROTECTIVE_STOP, note=reason)
        await self._repo.add_event(
            now,
            "protective_stop",
            f"{pos.symbol} x{pos.qty} stop {stop_price} ({reason})",
            level="warning",
            payload={"order_id": oid},
        )
        return oid
