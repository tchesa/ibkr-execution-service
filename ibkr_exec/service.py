"""Composition root and lifecycle: the one object the API and the jobs talk to.

Owns the broker, repository, risk engine inputs, bracket manager, flattener,
reconciler, halt state and readiness. Startup refuses to run when the config mode and
the connected account disagree, and nothing is accepted before the first reconcile.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time, timedelta
from typing import Any

from ibkr_exec.alerts.slack import Alerter
from ibkr_exec.api.schemas import DecisionOut, SignalIn, SignalStatusOut
from ibkr_exec.broker.base import Broker, BrokerError
from ibkr_exec.calendar import is_early_close, is_trading_day, ny, ny_date, session_close
from ibkr_exec.clock import Clock
from ibkr_exec.config import Config
from ibkr_exec.db.repo import Repository
from ibkr_exec.domain import (
    AccountState,
    BrokerOrder,
    Decision,
    Fill,
    HaltState,
    OrderRole,
    Signal,
    Warning,
)
from ibkr_exec.jobs.reconcile import Reconciler
from ibkr_exec.logging import get_logger
from ibkr_exec.orders.bracket import BracketManager
from ibkr_exec.orders.flatten import Flattener
from ibkr_exec.risk import pdt
from ibkr_exec.risk.engine import RiskState, check, daily_loss_breached

log = get_logger(__name__)


class ModeMismatch(RuntimeError):
    pass


class Service:
    def __init__(
        self,
        cfg: Config,
        broker: Broker,
        repo: Repository,
        clock: Clock,
        alerter: Alerter,
        env: dict[str, str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.repo = repo
        self.clock = clock
        self.alerter = alerter
        self.env = dict(os.environ if env is None else env)
        self.bracket = BracketManager(broker, repo, clock, cfg, alerter)
        self.flattener = Flattener(broker, repo, clock, cfg, alerter)
        self.reconciler = Reconciler(self)
        self.ready = False
        self.halt = HaltState(False)
        self.started_at: datetime | None = None
        self.last_reconcile_at: datetime | None = None
        self._disconnected_at: datetime | None = None
        self._disconnect_alerted = False
        self._next_reconnect_at: datetime | None = None
        self._reconnect_delay_s = 5.0
        self._signal_lock = asyncio.Lock()
        self._flattened_on: date | None = None
        self._eod_done_on: date | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def verify_mode(self, account_id: str) -> None:
        is_paper = account_id.upper().startswith("DU")
        if self.cfg.mode == "paper" and not is_paper:
            raise ModeMismatch(
                f"config mode is paper but connected account {account_id!r} is not a DU account"
            )
        if self.cfg.mode == "live":
            if is_paper:
                raise ModeMismatch(f"config mode is live but account {account_id!r} is paper")
            if self.env.get("CONFIRM_LIVE") != "yes":
                raise ModeMismatch("live mode requires CONFIRM_LIVE=yes in the environment")

    async def start(self) -> None:
        self.started_at = self.clock.now()
        await self.broker.connect()
        acct = await self.broker.account_state()
        self.verify_mode(acct.account_id)
        self.halt = await self.repo.get_halt()
        self.broker.on_order_status(self._on_status)
        self.broker.on_fill(self._on_fill)
        self.broker.on_warning(self._on_warning)
        self.broker.on_disconnect(self._on_disconnect)
        await self.reconciler.run("startup")
        self.ready = True
        await self.repo.add_event(
            self.clock.now(),
            "session",
            "service started",
            payload={"mode": self.cfg.mode, "account": acct.account_id, "halted": self.halt.halted},
        )
        log.info(
            "service_ready", mode=self.cfg.mode, account=acct.account_id, halted=self.halt.halted
        )

    async def stop(self) -> None:
        self.ready = False
        try:
            await self.repo.add_event(self.clock.now(), "session", "service stopping")
        finally:
            await self.broker.disconnect()

    # ── signals ───────────────────────────────────────────────────────────
    async def gather_risk_state(self, signal: Signal) -> RiskState:
        acct = await self.broker.account_state()
        positions = tuple(await self.broker.positions())
        orders_today = tuple(await self.broker.orders_today())
        try:
            quote = await self.broker.quote(signal.symbol)
        except BrokerError as exc:
            log.warning("quote_unavailable", symbol=signal.symbol, error=str(exc))
            quote = None
        day_trades = 0
        if self.cfg.risk.pdt_guard:
            start, end = pdt.pdt_window(
                ny_date(self.clock.now()), self.cfg.risk.pdt_window_business_days
            )
            day_trades = pdt.count_day_trades(await self.repo.fills_between(start, end))
        return RiskState(acct, positions, orders_today, quote, self.halt, day_trades)

    async def submit_signal(self, payload: SignalIn) -> DecisionOut:
        async with self._signal_lock:
            return await self._submit(payload)

    async def _submit(self, payload: SignalIn) -> DecisionOut:
        now = self.clock.now()
        sid = payload.signal_id
        existing = await self.repo.get_signal(sid)
        if existing is not None:
            return self._row_to_decision(existing, duplicate=True)

        signal = payload.to_signal()
        raw = payload.model_dump(mode="json")

        # Broker-side idempotency: a crash between placing and recording must not re-place.
        at_broker = await self.bracket.find_existing(sid)
        if at_broker:
            parent = next((o for o in at_broker if o.parent_id is None), at_broker[0])
            decision = Decision(
                True,
                parent.qty,
                ("order already at broker with this orderRef",),
                (),
                is_exit=parent.action == "SELL",
            )
            await self.repo.store_signal(signal, decision, now, raw)
            for o in at_broker:
                await self.repo.upsert_order(o, role=self.bracket._guess_role(o), signal_id=sid)
            await self.repo.set_signal_outcome(sid, parent_order_id=parent.order_id)
            return DecisionOut(
                signal_id=sid,
                decision="accepted",
                reasons=list(decision.reasons),
                order_id=parent.order_id,
                qty=parent.qty,
                duplicate=True,
            )

        state = await self.gather_risk_state(signal)
        decision = check(signal, state, self.cfg, now)
        await self.repo.store_signal(signal, decision, now, raw)
        log.info(
            "signal_decided",
            signal_id=sid,
            symbol=signal.symbol,
            side=signal.side.value,
            accepted=decision.accepted,
            qty=decision.qty,
            reasons=list(decision.reasons),
        )
        if not decision.accepted:
            return DecisionOut(signal_id=sid, decision="rejected", reasons=list(decision.reasons))

        if decision.is_exit:
            results = await self.flattener.flatten(
                signal.symbol, f"exit signal {sid}", qty=decision.qty, ref=sid
            )
            res = results[0] if results else None
            if res is None or not res.ok:
                err = res.error if res else "no position to close"
                await self.repo.set_signal_outcome(
                    sid, error=err, decision="rejected", extra_reason=f"exit failed: {err}"
                )
                return DecisionOut(
                    signal_id=sid,
                    decision="rejected",
                    reasons=[*decision.reasons, f"exit failed: {err}"],
                    error=err,
                )
            await self.repo.set_signal_outcome(sid, parent_order_id=res.sell_order_id)
            return DecisionOut(
                signal_id=sid,
                decision="accepted",
                reasons=[],
                order_id=res.sell_order_id,
                qty=res.qty,
            )

        try:
            ids = await self.bracket.place_entry(signal, decision.qty)
        except BrokerError as exc:
            err = str(exc)
            await self.repo.set_signal_outcome(
                sid, error=err, decision="rejected", extra_reason=f"broker: {err}"
            )
            await self.alerter.alert("critical", "Entry placement failed", err, signal_id=sid)
            return DecisionOut(
                signal_id=sid, decision="rejected", reasons=[f"broker: {err}"], error=err
            )
        return DecisionOut(
            signal_id=sid, decision="accepted", reasons=[], order_id=ids.parent_id, qty=decision.qty
        )

    @staticmethod
    def _row_to_decision(row: Any, duplicate: bool = False) -> DecisionOut:
        return DecisionOut(
            signal_id=row.signal_id,
            decision=row.decision,
            reasons=list(row.reasons.get("reasons", [])),
            order_id=row.parent_order_id,
            qty=row.qty if row.decision == "accepted" else None,
            duplicate=duplicate,
            error=row.error,
        )

    async def get_signal(self, signal_id: str) -> SignalStatusOut | None:
        row = await self.repo.get_signal(signal_id)
        if row is None:
            return None
        base = self._row_to_decision(row)
        orders = [
            {
                "order_id": o.order_id,
                "role": o.role,
                "status": o.status,
                "qty": o.qty,
                "filled": o.filled,
                "limit_price": o.limit_price,
                "stop_price": o.stop_price,
            }
            for o in await self.repo.orders_for_ref(signal_id)
        ]
        return SignalStatusOut(
            **base.model_dump(),
            received_at=row.received_at,
            rule_results=list(row.rule_results.get("rules", [])),
            orders=orders,
        )

    # ── halt ──────────────────────────────────────────────────────────────
    async def set_halt(self, reason: str, source: str) -> HaltState:
        now = self.clock.now()
        self.halt = await self.repo.set_halt(True, reason, now)
        await self.repo.add_event(now, "halt", f"halt on ({source}): {reason}", level="warning")
        await self.alerter.alert("critical", "HALT tripped", reason, source=source)
        return self.halt

    async def resume(self, source: str) -> HaltState:
        now = self.clock.now()
        self.halt = await self.repo.set_halt(False, None, now)
        await self.repo.add_event(now, "halt", f"halt off ({source})", level="warning")
        await self.alerter.alert(
            "warning", "Halt lifted", "new entries allowed again", source=source
        )
        return self.halt

    async def check_daily_loss(self, acct: AccountState) -> bool:
        if self.halt.halted or not daily_loss_breached(acct, self.cfg):
            return False
        await self.set_halt(
            f"daily realized loss {acct.realized_pnl_today:.2f} reached "
            f"-{self.cfg.risk.max_daily_loss_usd:g}",
            "daily_loss",
        )
        return True

    # ── broker callbacks ──────────────────────────────────────────────────
    async def _on_status(self, o: BrokerOrder) -> None:
        try:
            await self.bracket.handle_status(o)
        except Exception as exc:
            log.error("status_handler_failed", order_id=o.order_id, error=str(exc))

    async def _on_fill(self, f: Fill) -> None:
        try:
            await self.repo.upsert_fill(f)
            acct = await self.broker.account_state()
            await self.check_daily_loss(acct)
        except Exception as exc:
            log.error("fill_handler_failed", exec_id=f.exec_id, error=str(exc))

    async def _on_warning(self, w: Warning) -> None:
        try:
            await self.bracket.handle_warning(w)
        except Exception as exc:
            log.error("warning_handler_failed", code=w.code, error=str(exc))

    async def _on_disconnect(self) -> None:
        now = self.clock.now()
        if self._disconnected_at is None:
            self._disconnected_at = now
        log.warning("gateway_disconnected")
        await self.repo.add_event(now, "session", "gateway disconnected", level="warning")

    # ── jobs ──────────────────────────────────────────────────────────────
    async def job_keepalive(self) -> None:
        now = self.clock.now()
        if self.broker.is_connected():
            try:
                await asyncio.wait_for(self.broker.server_time(), timeout=10)
                if self._disconnected_at is not None:
                    await self._recovered()
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("keepalive_failed", error=str(exc))
                if self._disconnected_at is None:
                    self._disconnected_at = now
        elif self._disconnected_at is None:
            self._disconnected_at = now

        down_for = (now - self._disconnected_at).total_seconds()
        if down_for >= self.cfg.jobs.disconnect_alert_after_s and not self._disconnect_alerted:
            self._disconnect_alerted = True
            await self.alerter.alert(
                "critical",
                "Gateway session lost",
                f"disconnected for {int(down_for)}s; if IB Key approval is pending, approve it in "
                "IBKR Mobile. The service waits and does not retry logins in a loop.",
            )
        if self._next_reconnect_at and now < self._next_reconnect_at:
            return
        try:
            await self.broker.connect()
            await self._recovered()
        except Exception as exc:  # noqa: BLE001
            self._next_reconnect_at = now + timedelta(seconds=self._reconnect_delay_s)
            self._reconnect_delay_s = min(self._reconnect_delay_s * 2, 300)
            log.warning("reconnect_failed", error=str(exc), retry_in_s=self._reconnect_delay_s)

    async def _recovered(self) -> None:
        was_alerted = self._disconnect_alerted
        self._disconnected_at = None
        self._disconnect_alerted = False
        self._next_reconnect_at = None
        self._reconnect_delay_s = 5.0
        await self.repo.add_event(self.clock.now(), "session", "gateway reconnected")
        await self.reconciler.run("reconnect")
        if was_alerted:
            await self.alerter.alert("info", "Gateway reconnected", "reconciled after reconnect")

    def in_reconcile_window(self, now: datetime | None = None) -> bool:
        now = now or self.clock.now()
        local = ny(now)
        if not is_trading_day(local.date()):
            return False
        start, end = self.cfg.jobs.reconcile_window
        return start <= local.time() < end

    async def job_reconcile(self) -> None:
        if not self.broker.is_connected() or not self.in_reconcile_window():
            return
        await self.reconciler.run("scheduled")

    async def job_stale_entries(self) -> None:
        if self.broker.is_connected():
            await self.bracket.cancel_stale_parents()

    def flatten_time(self, d: date) -> time | None:
        """Local time to flatten on ``d``: flat_by, or the same lead before an early close."""
        close = session_close(d)
        if close is None:
            return None
        flat_by = self.cfg.risk.flat_by
        if not is_early_close(d):
            return flat_by
        lead = datetime.combine(d, time(16, 0)) - datetime.combine(d, flat_by)
        return (datetime.combine(d, close) - lead).time()

    async def job_flatten(self, force: bool = False) -> None:
        now = self.clock.now()
        local = ny(now)
        if not force:
            target = self.flatten_time(local.date())
            close = session_close(local.date())
            if target is None or close is None or self._flattened_on == local.date():
                return
            if not (target <= local.time() <= close):
                return
        self._flattened_on = local.date()
        results = await self.flattener.flatten(None, "scheduled flatten")
        failed = [r for r in results if not r.ok]
        for o in await self.broker.open_orders():
            if o.parent_id is None and o.action == "BUY" and o.status.is_working:
                try:
                    await self.broker.cancel(o.order_id)
                except BrokerError as exc:
                    await self.alerter.alert(
                        "warning", "Flatten: could not cancel entry", str(exc), order_id=o.order_id
                    )
        log.info("flatten_job", closed=len(results) - len(failed), failed=len(failed))

    async def job_eod(self, force: bool = False) -> None:
        now = self.clock.now()
        local = ny(now)
        today = local.date()
        if not force:
            close = session_close(today)
            if close is None or self._eod_done_on == today:
                return
            earliest = (datetime.combine(today, close) + timedelta(minutes=10)).time()
            if local.time() < earliest:
                return
        self._eod_done_on = today
        positions = [p for p in await self.broker.positions() if p.qty != 0]
        open_orders = await self.broker.open_orders()
        if positions:
            await self.alerter.alert(
                "critical",
                "Positions remaining after the close",
                ", ".join(f"{p.symbol} x{p.qty}" for p in positions),
            )
        working = [o for o in open_orders if o.status.is_working]
        if working and not positions:
            # Stray working orders with nothing to protect: cancel them.
            for o in working:
                try:
                    await self.broker.cancel(o.order_id)
                except BrokerError as exc:
                    await self.alerter.alert(
                        "warning", "EOD: could not cancel order", str(exc), order_id=o.order_id
                    )
            await self.alerter.alert(
                "warning",
                "Open orders remained after the close",
                f"cancelled {len(working)} working order(s)",
            )
        acct = await self.broker.account_state()
        fills = await self.repo.fills_between(today, today)
        commissions = sum(f.commission for f in fills)
        trades = sum(
            1
            for r in await self.repo.orders_on(today)
            if r.role == OrderRole.PARENT.value and r.filled > 0
        )
        halt_tripped = (
            self.halt.halted and self.halt.since is not None and ny_date(self.halt.since) == today
        )
        await self.repo.upsert_daily_pnl(
            today,
            realized_pnl=acct.realized_pnl_today,
            commissions=commissions,
            trades=trades,
            halt_tripped=halt_tripped,
            at=now,
        )
        await self.alerter.alert(
            "info",
            f"Daily summary {today.isoformat()}",
            f"realized {acct.realized_pnl_today:+.2f} USD, commissions {commissions:.2f}, "
            f"trades {trades}, fills {len(fills)}, halt {'tripped' if halt_tripped else 'off'}, "
            f"equity {acct.net_liquidation:.2f}",
        )

    async def job_preopen(self, force: bool = False) -> dict[str, Any]:
        now = self.clock.now()
        local = ny(now)
        if not force and not is_trading_day(local.date()):
            return {"skipped": "not a session"}
        problems: list[str] = []
        if not self.broker.is_connected():
            problems.append("gateway not connected")
            acct = None
        else:
            try:
                acct = await self.broker.account_state()
                self.verify_mode(acct.account_id)
            except (BrokerError, ModeMismatch) as exc:
                problems.append(f"account check failed: {exc}")
                acct = None
            try:
                q = await self.broker.quote(self.cfg.jobs.quote_symbol)
                if q.delayed:
                    problems.append(f"{q.symbol} market data is delayed, not live")
            except BrokerError as exc:
                problems.append(f"no market data for {self.cfg.jobs.quote_symbol}: {exc}")
        if self.halt.halted:
            problems.append(f"halt is on: {self.halt.reason} (manual /resume required)")
        result = {
            "ok": not problems,
            "problems": problems,
            "mode": self.cfg.mode,
            "account": acct.account_id if acct else None,
        }
        if problems:
            await self.alerter.alert("critical", "Pre-open check failed", "; ".join(problems))
        else:
            await self.repo.add_event(now, "preopen", "pre-open check passed", payload=result)
        return result

    # ── health ────────────────────────────────────────────────────────────
    async def health(self) -> dict[str, Any]:
        now = self.clock.now()
        since = self.broker.connected_since()
        pnl = None
        if self.broker.is_connected():
            try:
                pnl = (await self.broker.account_state()).realized_pnl_today
            except BrokerError:
                pnl = None
        return {
            "ready": self.ready,
            "mode": self.cfg.mode,
            "gateway_connected": self.broker.is_connected(),
            "session_age_s": int((now - since).total_seconds()) if since else None,
            "last_reconcile_at": self.last_reconcile_at.isoformat()
            if self.last_reconcile_at
            else None,
            "halted": self.halt.halted,
            "halt_reason": self.halt.reason,
            "realized_pnl_today": pnl,
            "time": now.isoformat(),
        }
