"""Request/response models for the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ibkr_exec.domain import Entry, EntryType, Side, Signal


class EntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["limit", "market"] = "limit"
    price: float = Field(gt=0)


class SignalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Za-z.]+$")
    side: Literal["buy", "sell"]
    entry: EntryIn
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    risk_usd: float = Field(gt=0, default=0.0)
    expires_at: datetime
    strategy: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=1000)
    qty: int | None = Field(default=None, gt=0)

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("expires_at must include a timezone offset")
        return v

    @field_validator("symbol")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def to_signal(self) -> Signal:
        return Signal(
            signal_id=self.signal_id,
            symbol=self.symbol,
            side=Side(self.side),
            entry=Entry(EntryType(self.entry.type), self.entry.price),
            stop=self.stop,
            target=self.target,
            risk_usd=self.risk_usd,
            expires_at=self.expires_at,
            strategy=self.strategy,
            reason=self.reason,
            qty=self.qty,
        )


class DecisionOut(BaseModel):
    signal_id: str
    decision: Literal["accepted", "rejected"]
    reasons: list[str]
    order_id: int | None = None
    qty: int | None = None
    duplicate: bool = False
    error: str | None = None


class SignalStatusOut(DecisionOut):
    received_at: datetime | None = None
    rule_results: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []


class FlattenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = Field(default=None, max_length=16)
    reason: str = Field(default="manual", max_length=200)


class HaltIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="manual", max_length=200)
