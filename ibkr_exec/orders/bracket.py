"""Bracket entries: build, place idempotently, resize on partial fills, cancel when stale.

Every entry goes to the broker as one bracket so a stop sits there from the moment the
entry can fill. The parent is a DAY limit buy in regular hours; the stop and target are
GTC sells in one OCA group.
"""

from __future__ import annotations

from datetime import timedelta

from ibkr_exec.alerts.slack import Alerter
from ibkr_exec.broker.base import Broker, BrokerError
from ibkr_exec.calendar import is_early_close, ny
from ibkr_exec.clock import Clock
from ibkr_exec.config import Config
from ibkr_exec.db.repo import Repository
from ibkr_exec.domain import (
    BracketIds,
    BracketRequest,
    BrokerOrder,
    OrderRole,
    OrderStatus,
    Signal,
    Warning,
)
from ibkr_exec.logging import get_logger

log = get_logger(__name__)


def _tick(price: float) -> float:
    return round(price, 2)


class BracketManager:
    def __init__(
        self, broker: Broker, repo: Repository, clock: Clock, cfg: Config, alerter: Alerter
    ) -> None:
        self._b = broker
        self._repo = repo
        self._clock = clock
        self._cfg = cfg
        self._alerts = alerter

    # ── placement ─────────────────────────────────────────────────────────
    async def find_existing(self, signal_id: str) -> list[BrokerOrder]:
        """Broker-side orders carrying this signal's orderRef (today's and open)."""
        seen: dict[int, BrokerOrder] = {}
        for o in await self._b.orders_today():
            if o.order_ref == signal_id:
                seen[o.order_id] = o
        for o in await self._b.open_orders():
            if o.order_ref == signal_id:
                seen[o.order_id] = o
        return sorted(seen.values(), key=lambda o: o.order_id)

    async def place_entry(self, signal: Signal, qty: int) -> BracketIds:
        assert signal.stop is not None and signal.target is not None
        req = BracketRequest(
            signal_id=signal.signal_id,
            symbol=signal.symbol.upper(),
            qty=qty,
            limit_price=_tick(signal.entry.price),
            stop_price=_tick(signal.stop),
            target_price=_tick(signal.target),
        )
        ids = await self._b.place_bracket(req)
        now = self._clock.now()
        roles = {
            ids.parent_id: OrderRole.PARENT,
            ids.stop_id: OrderRole.STOP,
            ids.target_id: OrderRole.TARGET,
        }
        # Persist what we know immediately; status callbacks refine it.
        for o in await self.find_existing(signal.signal_id):
            await self._repo.upsert_order(o, role=roles.get(o.order_id), signal_id=signal.signal_id)
        await self._repo.set_signal_outcome(signal.signal_id, parent_order_id=ids.parent_id)
        await self._repo.add_event(
            now,
            "order_placed",
            f"bracket for {signal.symbol} x{qty}",
            payload={"signal_id": signal.signal_id, "parent_id": ids.parent_id},
        )
        log.info("bracket_placed", signal_id=signal.signal_id, qty=qty, parent_id=ids.parent_id)
        return ids

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def handle_status(self, o: BrokerOrder) -> None:
        """Order status from the broker: persist, resize children, record rejections."""
        row = await self._repo.get_order(o.order_id)
        role = None
        signal_id = row.signal_id if row else None
        if row is None:
            role = self._guess_role(o)
        await self._repo.upsert_order(o, role=role, signal_id=signal_id)

        if o.status is OrderStatus.REJECTED:
            await self._alerts.alert(
                "critical",
                "Order rejected",
                f"{o.symbol} {o.action} x{o.qty}",
                order_id=o.order_id,
                order_ref=o.order_ref,
            )
            return

        is_parent = o.parent_id is None and o.action == "BUY"
        if is_parent and 0 < o.filled:
            await self._resize_children(o)

    async def _resize_children(self, parent: BrokerOrder) -> None:
        children = [
            c
            for c in await self._b.open_orders()
            if c.parent_id == parent.order_id and c.status.is_working
        ]
        for c in children:
            if c.qty != parent.filled:
                log.info("resize_child", child=c.order_id, from_qty=c.qty, to_qty=parent.filled)
                await self._b.modify_qty(c.order_id, parent.filled)

    def _guess_role(self, o: BrokerOrder) -> OrderRole:
        if o.parent_id is None:
            return OrderRole.PARENT if o.action == "BUY" else OrderRole.FLATTEN
        return OrderRole.STOP if o.order_type == "STP" else OrderRole.TARGET

    async def cancel_stale_parents(self) -> list[int]:
        """Cancel unfilled parents past their timeout or past the entry window end."""
        now = self._clock.now()
        timeout = timedelta(minutes=self._cfg.risk.entry_timeout_min)
        local = ny(now)
        end = self._cfg.risk.entry_window[1]
        if is_early_close(local.date()):
            end = min(end, self._cfg.risk.early_close_entry_end)
        window_over = local.time() >= end
        cancelled: list[int] = []
        for o in await self._b.open_orders():
            if not (o.parent_id is None and o.action == "BUY" and o.status.is_working):
                continue
            age = now - o.created_at
            why = None
            if age >= timeout:
                why = f"unfilled after {int(age.total_seconds() // 60)} min"
            elif window_over:
                why = "entry window closed"
            if why is None:
                continue
            try:
                await self._b.cancel(o.order_id)
                cancelled.append(o.order_id)
                await self._repo.upsert_order(o, note=f"cancelled: {why}")
                await self._repo.add_event(
                    now,
                    "order_cancelled",
                    f"{o.symbol} parent {o.order_id}: {why}",
                    payload={"order_id": o.order_id, "filled": o.filled},
                )
                log.info("stale_parent_cancelled", order_id=o.order_id, why=why)
            except BrokerError as exc:
                await self._alerts.alert(
                    "warning", "Could not cancel stale entry", str(exc), order_id=o.order_id
                )
        return cancelled

    async def handle_warning(self, w: Warning) -> None:
        """IBKR precaution/warning: allow-listed codes are logged; anything else cancels."""
        now = self._clock.now()
        allowed = w.code in self._cfg.orders.warnings_allow
        await self._repo.add_event(
            now,
            "broker_warning",
            f"{w.code}: {w.message}",
            level="info" if allowed else "warning",
            payload={"code": w.code, "order_id": w.order_id},
        )
        if allowed or w.order_id is None:
            if not allowed:
                log.warning("broker_warning_unlisted", code=w.code, message=w.message)
            return
        # A warning on a specific order that we did not explicitly allow: never auto-accept.
        try:
            await self._b.cancel(w.order_id)
            outcome = "cancelled"
        except BrokerError as exc:
            outcome = f"cancel failed: {exc}"
        await self._alerts.alert(
            "critical",
            "Order held by broker warning",
            f"code {w.code}: {w.message}",
            order_id=w.order_id,
            outcome=outcome,
        )
