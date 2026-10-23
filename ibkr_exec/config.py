"""Load and validate config.yaml once at startup.

Limits live in the file, never in the API. ``${VAR}`` placeholders are expanded from
the environment at load time so secrets never sit in the file.
"""

from __future__ import annotations

import os
import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IBConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 17


class RiskConfig(_Strict):
    max_loss_per_trade_usd: float = 50
    max_daily_loss_usd: float = 150
    max_order_usd: float = 4000
    max_position_pct: float = 75
    cash_buffer_pct: float = 10
    max_trades_per_day: int = 3
    max_open_positions: int = 2
    min_reward_risk: float = 1.0
    entry_window: tuple[time, time] = (time(9, 45), time(15, 30))
    flat_by: time = time(15, 55)
    early_close_entry_end: time = time(12, 30)
    price_tolerance_pct: float = 1.0
    quote_max_age_s: float = 15
    entry_timeout_min: int = 5
    pdt_guard: bool = True
    pdt_equity_threshold_usd: float = 25_000
    pdt_max_day_trades: int = 3
    pdt_window_business_days: int = 5

    @field_validator("entry_window", mode="before")
    @classmethod
    def _parse_window(cls, v: object) -> object:
        if isinstance(v, list | tuple) and len(v) == 2:
            return tuple(_parse_time(x) for x in v)
        return v

    @field_validator("flat_by", "early_close_entry_end", mode="before")
    @classmethod
    def _parse_t(cls, v: object) -> object:
        return _parse_time(v)

    @model_validator(mode="after")
    def _ordered(self) -> RiskConfig:
        start, end = self.entry_window
        if not start < end:
            raise ValueError("entry_window start must be before end")
        if not end <= self.flat_by:
            raise ValueError("entry_window end must not be after flat_by")
        for name in (
            "max_loss_per_trade_usd",
            "max_daily_loss_usd",
            "max_order_usd",
            "price_tolerance_pct",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"risk.{name} must be positive")
        if not 0 < self.max_position_pct <= 100:
            raise ValueError("risk.max_position_pct must be in (0, 100]")
        if not 0 <= self.cash_buffer_pct < 100:
            raise ValueError("risk.cash_buffer_pct must be in [0, 100)")
        return self


class SymbolsConfig(_Strict):
    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = ()

    @field_validator("allow", "deny", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        if isinstance(v, list | tuple):
            return tuple(str(s).upper() for s in v)
        return v

    def is_allowed(self, symbol: str) -> bool:
        s = symbol.upper()
        if s in self.deny:
            return False
        return "*" in self.allow or s in self.allow


class AlertsConfig(_Strict):
    slack_webhook_url: str = ""

    @field_validator("slack_webhook_url", mode="before")
    @classmethod
    def _none_to_empty(cls, v: object) -> object:
        return "" if v is None else v


class OrdersConfig(_Strict):
    # IBKR error/warning codes that may accompany an order without cancelling it.
    # 2104/2106/2158: market data farm connection notices. 2109: order outside RTH
    # attribute ignored (we never set it). 399 is "order message" and is NOT allowed.
    warnings_allow: tuple[int, ...] = (2104, 2106, 2107, 2108, 2158, 2109, 2100)
    marketable_offset_pct: float = 0.2  # flatten sells at last * (1 - this/100)
    cancel_timeout_s: float = 10
    place_timeout_s: float = 10


class JobsConfig(_Strict):
    reconcile_window: tuple[time, time] = (time(9, 25), time(16, 5))
    eod_check: time = time(16, 10)
    preopen_check: time = time(9, 15)
    disconnect_alert_after_s: float = 120
    quote_symbol: str = "SPY"  # used by the pre-open market data check

    @field_validator("reconcile_window", mode="before")
    @classmethod
    def _parse_window(cls, v: object) -> object:
        if isinstance(v, list | tuple) and len(v) == 2:
            return tuple(_parse_time(x) for x in v)
        return v

    @field_validator("eod_check", "preopen_check", mode="before")
    @classmethod
    def _parse_t(cls, v: object) -> object:
        return _parse_time(v)


class Config(_Strict):
    mode: Literal["paper", "live"] = "paper"
    ib: IBConfig = Field(default_factory=IBConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    symbols: SymbolsConfig = Field(default_factory=SymbolsConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    orders: OrdersConfig = Field(default_factory=OrdersConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)

    @property
    def expected_account_prefix(self) -> str:
        return "DU" if self.mode == "paper" else "U"


def _parse_time(v: object) -> object:
    if isinstance(v, time):
        return v
    if isinstance(v, str):
        hh, mm = v.split(":")
        return time(int(hh), int(mm))
    return v


def expand_env(text: str, env: dict[str, str] | None = None) -> str:
    """Replace ``${NAME}`` and ``${NAME:-default}`` with environment values.

    A missing variable with no default expands to an empty string so a secret that is
    not set fails downstream validation instead of leaking the placeholder.
    """
    env = os.environ if env is None else env

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        return env.get(name, default if default is not None else "")

    return _ENV_RE.sub(repl, text)


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    raw = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(expand_env(raw, env)) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must be a mapping")
    return Config.model_validate(data)
