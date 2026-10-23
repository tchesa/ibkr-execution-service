"""Reconcile: make the database match the broker and fix what is unsafe.

Runs every 60 s in market hours, on every reconnect, and before the service reports
ready. Drift (an unknown position or order, a position with no working stop) alerts;
an unprotected position gets a protective stop immediately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from ibkr_exec.calendar import ny_date
from ibkr_exec.domain import BrokerOrder, OrderRole, Position
from ibkr_exec.logging import get_logger

if TYPE_CHECKING:
    from ibkr_exec.service import Service

log = get_logger(__name__)


@dataclass(frozen=True)
class ReconcileReport:
    at: datetime
    positions: int
    open_orders: int
    new_fills: int
    unknown_positions: tuple[str, ...]
    unknown_orders: tuple[int, ...]
    stops_placed: tuple[str, ...]
    halt_tripped: bool


@dataclass
class Reconciler:
    svc: Service
    _alerted: set[tuple[str, str, date]] = field(default_factory=set)

    async def run(self, reason: str) -> ReconcileReport:
        svc = self.svc
        now = svc.clock.now()
        today = ny_date(now)
        positions = [p for p in await svc.broker.positions() if p.qty != 0]
        open_orders = await svc.broker.open_orders()

        # Orders: persist everything the broker has open; flag the ones we never placed.
        known_ids = await svc.repo.known_order_ids()
        unknown_orders: list[int] = []
        for o in open_orders:
            if o.order_id not in known_ids:
                unknown_orders.append(o.order_id)
                await svc.repo.upsert_order(
                    o, role=svc.bracket._guess_role(o), note="seen at reconcile"
                )
                await self._drift(
                    "unknown_order",
                    str(o.order_id),
                    today,
                    f"{o.symbol} {o.action} {o.order_type} x{o.qty} ref={o.order_ref!r}",
                )
            else:
                await svc.repo.upsert_order(o)

        # Fills since the last one we know (with a small overlap).
        last = await svc.repo.last_fill_time()
        since = last - timedelta(minutes=1) if last else None
        new_fills = 0
        for f in await svc.broker.executions_since(since):
            if await svc.repo.upsert_fill(f):
                new_fills += 1

        # Positions: unknown ones, and every open position needs a working stop.
        known_syms = await svc.repo.known_symbols()
        unknown_positions: list[str] = []
        stops_placed: list[str] = []
        has_stop: dict[str, bool] = {}
        for p in positions:
            sym = p.symbol.upper()
            if sym not in known_syms:
                unknown_positions.append(sym)
                await self._drift("unknown_position", sym, today, f"{sym} x{p.qty} @ {p.avg_cost}")
            if p.qty > 0:
                protected = await self._ensure_stop(p, open_orders, today)
                has_stop[sym] = protected is None
                if protected:
                    stops_placed.append(sym)
            else:
                # A short position should be impossible on this account.
                await self._drift("short_position", sym, today, f"{sym} x{p.qty}")
                has_stop[sym] = False

        await svc.repo.snapshot_positions(now, positions, has_stop)

        # Daily loss limit trips the halt.
        acct = await svc.broker.account_state()
        tripped = await svc.check_daily_loss(acct)

        await svc.repo.set_last_reconcile(now)
        svc.last_reconcile_at = now
        report = ReconcileReport(
            now,
            len(positions),
            len(open_orders),
            new_fills,
            tuple(unknown_positions),
            tuple(unknown_orders),
            tuple(stops_placed),
            tripped,
        )
        log.info(
            "reconciled",
            reason=reason,
            positions=len(positions),
            open_orders=len(open_orders),
            new_fills=new_fills,
            unknown_positions=unknown_positions,
            unknown_orders=unknown_orders,
            stops_placed=stops_placed,
        )
        return report

    async def _ensure_stop(
        self, p: Position, open_orders: list[BrokerOrder], today: date
    ) -> str | None:
        """Return a description if a stop had to be placed/resized, else None."""
        svc = self.svc
        stops = [
            o
            for o in open_orders
            if o.symbol.upper() == p.symbol.upper()
            and o.action == "SELL"
            and o.order_type == "STP"
            and o.status.is_working
        ]
        covered = sum(o.remaining for o in stops)
        if covered >= p.qty:
            return None
        if stops:
            # Grow the existing stop to cover the whole position.
            biggest = max(stops, key=lambda o: o.remaining)
            await svc.broker.modify_qty(biggest.order_id, biggest.qty + (p.qty - covered))
            await self._drift(
                "stop_undersized",
                p.symbol,
                today,
                f"{p.symbol}: stop covered {covered} of {p.qty}; resized",
            )
            return f"{p.symbol}: resized"
        stop_px = await self._signal_stop(p.symbol, today)
        await self._drift(
            "missing_stop", p.symbol, today, f"{p.symbol} x{p.qty} had no working stop"
        )
        oid = await svc.flattener.place_protective_stop(p, stop_px, "reconcile: missing stop")
        return f"{p.symbol}: placed {oid}"

    async def _signal_stop(self, symbol: str, today: date) -> float | None:
        """The stop price from today's entry signal for this symbol, if we have it."""
        for row in await self.svc.repo.orders_for_symbol_on(symbol, today):
            if row.role == OrderRole.PARENT.value and row.signal_id:
                sig = await self.svc.repo.get_signal(row.signal_id)
                if sig and isinstance(sig.payload, dict) and sig.payload.get("stop"):
                    return float(sig.payload["stop"])
        return None

    async def _drift(self, kind: str, key: str, today: date, detail: str) -> None:
        marker = (kind, key, today)
        if marker in self._alerted:
            return
        self._alerted.add(marker)
        await self.svc.alerter.alert("critical", f"Reconcile drift: {kind}", detail, kind=kind)
