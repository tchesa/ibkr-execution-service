"""Repository: the only module that issues SQL. Everything else talks in domain types.

The database records intent and history; IBKR stays the source of truth, so the
repository is corrected to match the broker and never the reverse.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ibkr_exec.calendar import ny_date
from ibkr_exec.db.models import (
    Base,
    DailyPnlRow,
    EventRow,
    FillRow,
    OrderRow,
    PositionSnapshotRow,
    ServiceStateRow,
    SignalRow,
)
from ibkr_exec.domain import (
    BrokerOrder,
    Decision,
    Fill,
    HaltState,
    OrderRole,
    OrderStatus,
    Position,
    Signal,
)


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


class Repository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Create tables directly (tests and the fake broker runs); production uses Alembic."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    # ── signals ───────────────────────────────────────────────────────────
    async def get_signal(self, signal_id: str) -> SignalRow | None:
        async with self._sessions() as s:
            return await s.get(SignalRow, signal_id)

    async def store_signal(
        self,
        signal: Signal,
        decision: Decision,
        received_at: datetime,
        payload: dict[str, Any],
    ) -> SignalRow:
        row = SignalRow(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side.value,
            strategy=signal.strategy,
            payload=payload,
            received_at=received_at,
            ny_date=ny_date(received_at),
            decision="accepted" if decision.accepted else "rejected",
            qty=decision.qty,
            reasons={"reasons": list(decision.reasons)},
            rule_results={"rules": [asdict(r) for r in decision.rule_results]},
            is_exit=decision.is_exit,
        )
        async with self._sessions() as s:
            s.add(row)
            await s.commit()
        return row

    async def set_signal_outcome(
        self,
        signal_id: str,
        *,
        parent_order_id: int | None = None,
        error: str | None = None,
        decision: str | None = None,
        extra_reason: str | None = None,
    ) -> None:
        async with self._sessions() as s:
            row = await s.get(SignalRow, signal_id)
            if row is None:
                return
            row.parent_order_id = parent_order_id
            row.error = error
            if decision is not None:
                row.decision = decision
            if extra_reason:
                row.reasons = {"reasons": [*row.reasons.get("reasons", []), extra_reason]}
            await s.commit()

    async def known_symbols(self) -> set[str]:
        async with self._sessions() as s:
            res = await s.scalars(select(OrderRow.symbol).distinct())
            return {sym.upper() for sym in res}

    async def orders_for_symbol_on(self, symbol: str, d: date) -> list[OrderRow]:
        async with self._sessions() as s:
            res = await s.scalars(
                select(OrderRow)
                .where(OrderRow.symbol == symbol.upper(), OrderRow.ny_date == d)
                .order_by(OrderRow.order_id)
            )
            return list(res)

    async def signals_on(self, d: date) -> list[SignalRow]:
        async with self._sessions() as s:
            res = await s.scalars(select(SignalRow).where(SignalRow.ny_date == d))
            return list(res)

    # ── orders ────────────────────────────────────────────────────────────
    async def upsert_order(
        self,
        o: BrokerOrder,
        role: OrderRole | None = None,
        signal_id: str | None = None,
        note: str | None = None,
    ) -> None:
        async with self._sessions() as s:
            row = await s.get(OrderRow, o.order_id)
            if row is None:
                row = OrderRow(
                    order_id=o.order_id,
                    role=(role or OrderRole.PARENT).value,
                    created_at=o.created_at,
                    ny_date=ny_date(o.created_at),
                )
                s.add(row)
            elif role is not None:
                row.role = role.value
            row.perm_id = o.perm_id
            if signal_id is not None:
                row.signal_id = signal_id
            row.order_ref = o.order_ref
            row.symbol = o.symbol
            row.action = o.action
            row.order_type = o.order_type
            row.status = o.status.value
            row.qty = o.qty
            row.filled = o.filled
            row.limit_price = o.limit_price
            row.stop_price = o.stop_price
            row.tif = o.tif
            row.parent_id = o.parent_id
            row.oca_group = o.oca_group
            row.updated_at = o.updated_at
            if note:
                row.note = note
            await s.commit()

    async def get_order(self, order_id: int) -> OrderRow | None:
        async with self._sessions() as s:
            return await s.get(OrderRow, order_id)

    async def orders_for_ref(self, order_ref: str) -> list[OrderRow]:
        async with self._sessions() as s:
            res = await s.scalars(
                select(OrderRow).where(OrderRow.order_ref == order_ref).order_by(OrderRow.order_id)
            )
            return list(res)

    async def working_orders(self) -> list[OrderRow]:
        working = [st.value for st in OrderStatus if st.is_working]
        async with self._sessions() as s:
            res = await s.scalars(select(OrderRow).where(OrderRow.status.in_(working)))
            return list(res)

    async def orders_on(self, d: date) -> list[OrderRow]:
        async with self._sessions() as s:
            res = await s.scalars(select(OrderRow).where(OrderRow.ny_date == d))
            return list(res)

    async def known_order_ids(self) -> set[int]:
        async with self._sessions() as s:
            res = await s.scalars(select(OrderRow.order_id))
            return set(res)

    # ── fills ─────────────────────────────────────────────────────────────
    async def upsert_fill(self, f: Fill) -> bool:
        """Insert a fill; returns False if the exec_id was already known."""
        async with self._sessions() as s:
            existing = await s.get(FillRow, f.exec_id)
            if existing is not None:
                # Commission reports can arrive after the execution; backfill once known.
                if existing.commission == 0 and f.commission:
                    existing.commission = f.commission
                    await s.commit()
                return False
            s.add(
                FillRow(
                    exec_id=f.exec_id,
                    order_id=f.order_id,
                    order_ref=f.order_ref,
                    symbol=f.symbol,
                    action=f.action,
                    qty=f.qty,
                    price=f.price,
                    commission=f.commission,
                    time=f.time,
                    ny_date=ny_date(f.time),
                )
            )
            await s.commit()
            return True

    async def fills_between(self, start: date, end: date) -> list[Fill]:
        """Fills whose New York date is in [start, end]."""
        async with self._sessions() as s:
            res = await s.scalars(
                select(FillRow)
                .where(FillRow.ny_date >= start, FillRow.ny_date <= end)
                .order_by(FillRow.time)
            )
            return [
                Fill(
                    exec_id=r.exec_id,
                    order_id=r.order_id,
                    symbol=r.symbol,
                    action=r.action,
                    qty=r.qty,
                    price=r.price,
                    commission=r.commission,
                    time=r.time,
                    order_ref=r.order_ref,
                )
                for r in res
            ]

    async def last_fill_time(self) -> datetime | None:
        async with self._sessions() as s:
            return await s.scalar(select(func.max(FillRow.time)))

    # ── snapshots / pnl / events ──────────────────────────────────────────
    async def snapshot_positions(
        self, at: datetime, positions: list[Position], stops: dict[str, bool]
    ) -> None:
        async with self._sessions() as s:
            for p in positions:
                s.add(
                    PositionSnapshotRow(
                        taken_at=at,
                        symbol=p.symbol,
                        qty=p.qty,
                        avg_cost=p.avg_cost,
                        has_stop=stops.get(p.symbol, False),
                    )
                )
            await s.commit()

    async def upsert_daily_pnl(
        self,
        d: date,
        *,
        realized_pnl: float,
        commissions: float,
        trades: int,
        halt_tripped: bool,
        at: datetime,
    ) -> None:
        async with self._sessions() as s:
            row = await s.get(DailyPnlRow, d)
            if row is None:
                row = DailyPnlRow(ny_date=d)
                s.add(row)
            row.realized_pnl = realized_pnl
            row.commissions = commissions
            row.trades = trades
            row.halt_tripped = halt_tripped
            row.written_at = at
            await s.commit()

    async def get_daily_pnl(self, d: date) -> DailyPnlRow | None:
        async with self._sessions() as s:
            return await s.get(DailyPnlRow, d)

    async def add_event(
        self,
        at: datetime,
        kind: str,
        message: str,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        async with self._sessions() as s:
            s.add(EventRow(at=at, kind=kind, level=level, message=message, payload=payload or {}))
            await s.commit()

    async def events_since(self, since: datetime) -> list[EventRow]:
        async with self._sessions() as s:
            res = await s.scalars(
                select(EventRow).where(EventRow.at >= since).order_by(EventRow.at)
            )
            return list(res)

    # ── service state ─────────────────────────────────────────────────────
    async def _state(self, s: AsyncSession) -> ServiceStateRow:
        row = await s.get(ServiceStateRow, 1)
        if row is None:
            row = ServiceStateRow(id=1, halted=False)
            s.add(row)
            await s.flush()
        return row

    async def get_halt(self) -> HaltState:
        async with self._sessions() as s:
            row = await self._state(s)
            await s.commit()
            return HaltState(row.halted, row.halt_reason, row.halted_at)

    async def set_halt(self, halted: bool, reason: str | None, at: datetime) -> HaltState:
        async with self._sessions() as s:
            row = await self._state(s)
            row.halted = halted
            row.halt_reason = reason if halted else None
            row.halted_at = at if halted else None
            await s.commit()
            return HaltState(row.halted, row.halt_reason, row.halted_at)

    async def set_last_reconcile(self, at: datetime) -> None:
        async with self._sessions() as s:
            row = await self._state(s)
            row.last_reconcile_at = at
            await s.commit()

    async def last_reconcile(self) -> datetime | None:
        async with self._sessions() as s:
            row = await self._state(s)
            await s.commit()
            return row.last_reconcile_at
