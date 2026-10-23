"""SQLAlchemy tables. All timestamps are stored as UTC (timezone-aware columns)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC in and out, whatever the backend does with offsets.

    Postgres keeps the offset (timestamptz). SQLite (tests) drops it, so naive values
    coming back are re-labelled as UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime written to the database")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: UTCDateTime()}


class SignalRow(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))
    strategy: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ny_date: Mapped[date] = mapped_column(Date, index=True)
    decision: Mapped[str] = mapped_column(String(16))  # accepted | rejected
    qty: Mapped[int] = mapped_column(Integer, default=0)
    reasons: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"reasons": [...]}
    rule_results: Mapped[dict[str, Any]] = mapped_column(JSON)  # {"rules": [...]}
    is_exit: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    perm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(
        ForeignKey("signals.signal_id"), nullable=True, index=True
    )
    order_ref: Mapped[str] = mapped_column(String(128), index=True)
    role: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    filled: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    tif: Mapped[str] = mapped_column(String(4), default="DAY")
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oca_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
    ny_date: Mapped[date] = mapped_column(Date, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FillRow(Base):
    __tablename__ = "fills"

    exec_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_ref: Mapped[str] = mapped_column(String(128), default="")
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    time: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ny_date: Mapped[date] = mapped_column(Date, index=True)


class PositionSnapshotRow(Base):
    __tablename__ = "positions_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    qty: Mapped[int] = mapped_column(Integer)
    avg_cost: Mapped[float] = mapped_column(Float)
    has_stop: Mapped[bool] = mapped_column(Boolean)


class DailyPnlRow(Base):
    __tablename__ = "daily_pnl"

    ny_date: Mapped[date] = mapped_column(Date, primary_key=True)
    realized_pnl: Mapped[float] = mapped_column(Float)
    commissions: Mapped[float] = mapped_column(Float)
    trades: Mapped[int] = mapped_column(Integer)
    halt_tripped: Mapped[bool] = mapped_column(Boolean)
    written_at: Mapped[datetime] = mapped_column(UTCDateTime())


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ServiceStateRow(Base):
    """Single row (id=1). The halt flag lives here so a tripped halt survives restarts."""

    __tablename__ = "service_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    halted: Mapped[bool] = mapped_column(Boolean, default=False)
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_reconcile_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


Index("ix_orders_symbol_status", OrderRow.symbol, OrderRow.status)
