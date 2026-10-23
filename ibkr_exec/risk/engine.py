"""The risk engine: ``check(signal, state, config, now) -> Decision``.

A pure function over broker-reported state. Every rule runs and is recorded, so a
rejection can be explained from the database alone. Entries must pass every rule;
exits (sells that only reduce a held position) are checked for format and quantity
only, so getting out is always possible.

Ported from day-trader-skill ``trader-risk/check.py`` and ``trader-budget/size.py``:
same rules, but state comes from the broker and the human "yes" is gone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from ibkr_exec.calendar import entry_window_open
from ibkr_exec.config import Config
from ibkr_exec.domain import (
    AccountState,
    BrokerOrder,
    Decision,
    HaltState,
    Position,
    Quote,
    RuleResult,
    Side,
    Signal,
)


@dataclass(frozen=True)
class RiskState:
    """Everything the rules look at, read live from the broker just before the check."""

    account: AccountState
    positions: tuple[Position, ...]
    orders_today: tuple[BrokerOrder, ...]  # all of today's orders, any status
    quote: Quote | None
    halt: HaltState
    day_trades_in_window: int = 0  # from fills over the PDT window, excluding today's entry


@dataclass(frozen=True)
class Sizing:
    qty: int
    effective_entry: float
    risk_per_share: float
    risk_usd_used: float
    caps: tuple[str, ...]  # which limits reduced the quantity


def daily_loss_breached(account: AccountState, cfg: Config) -> bool:
    return account.realized_pnl_today <= -cfg.risk.max_daily_loss_usd


def _is_entry_order(o: BrokerOrder) -> bool:
    """A parent BUY that filled (even partially) or is still working counts as an entry."""
    return o.action == "BUY" and o.parent_id is None and (o.filled > 0 or o.status.is_working)


def _held(positions: tuple[Position, ...], symbol: str) -> int:
    return sum(p.qty for p in positions if p.symbol.upper() == symbol.upper())


def size(signal: Signal, state: RiskState, cfg: Config) -> Sizing:
    """Quantity from risk, then capped by order value, position size and cash buffer.

    Risk is measured from the higher of the signal's entry and the live ask so a stale
    signal cannot understate it.
    """
    r = cfg.risk
    entry = signal.entry.price
    if state.quote is not None and state.quote.ask > 0:
        entry = max(entry, state.quote.ask)
    stop = signal.stop or 0.0
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return Sizing(0, entry, risk_per_share, 0.0, ("invalid_stop",))

    risk_usd = min(signal.risk_usd, r.max_loss_per_trade_usd)
    qty = math.floor(risk_usd / risk_per_share)
    caps: list[str] = []
    if signal.risk_usd > r.max_loss_per_trade_usd:
        caps.append(f"risk_usd capped to {r.max_loss_per_trade_usd:g}")

    def cap(limit_qty: int, name: str) -> None:
        nonlocal qty
        if limit_qty < qty:
            qty = max(limit_qty, 0)
            caps.append(name)

    cap(math.floor(r.max_order_usd / entry), "max_order_usd")
    cap(
        math.floor(r.max_position_pct / 100 * state.account.net_liquidation / entry),
        "max_position_pct",
    )
    committed = sum(
        o.remaining * (o.limit_price or entry)
        for o in state.orders_today
        if o.action == "BUY" and o.parent_id is None and o.status.is_working
    )
    spendable = state.account.available_funds * (1 - r.cash_buffer_pct / 100) - committed
    cap(math.floor(max(spendable, 0) / entry), "cash_buffer")
    return Sizing(qty, entry, risk_per_share, qty * risk_per_share, tuple(caps))


def check(signal: Signal, state: RiskState, cfg: Config, now: datetime) -> Decision:
    if signal.side is Side.SELL:
        return _check_exit(signal, state, cfg, now)
    return _check_entry(signal, state, cfg, now)


def _check_exit(signal: Signal, state: RiskState, cfg: Config, now: datetime) -> Decision:
    results: list[RuleResult] = []
    held = _held(state.positions, signal.symbol)
    results.append(
        RuleResult(
            "format",
            signal.symbol.isalnum() and cfg.symbols.is_allowed(signal.symbol),
            f"symbol {signal.symbol}",
        )
    )
    wanted = signal.qty if signal.qty is not None else held
    ok_qty = held > 0 and 0 < wanted <= held
    results.append(
        RuleResult(
            "exit_quantity",
            ok_qty,
            f"sell {wanted} vs broker position {held}"
            if held > 0
            else f"no open position in {signal.symbol}",
        )
    )
    accepted = all(r.passed for r in results)
    return Decision(
        accepted=accepted,
        qty=wanted if accepted else 0,
        reasons=tuple(r.detail for r in results if not r.passed),
        rule_results=tuple(results),
        is_exit=True,
    )


def _check_entry(signal: Signal, state: RiskState, cfg: Config, now: datetime) -> Decision:
    r = cfg.risk
    acct = state.account
    results: list[RuleResult] = []

    def rule(name: str, passed: bool, detail: str) -> None:
        results.append(RuleResult(name, bool(passed), detail))

    # Kill switch
    rule(
        "kill_switch",
        not state.halt.halted,
        f"halted: {state.halt.reason}" if state.halt.halted else "halt off",
    )

    # Daily realized loss
    rule(
        "daily_loss",
        not daily_loss_breached(acct, cfg),
        f"realized today {acct.realized_pnl_today:.2f} vs limit -{r.max_daily_loss_usd:g}",
    )

    # Allowed symbols
    rule(
        "allowed_symbol",
        signal.symbol.isalnum() and cfg.symbols.is_allowed(signal.symbol),
        f"symbol {signal.symbol}",
    )

    # Signal freshness
    rule(
        "signal_freshness",
        now <= signal.expires_at,
        f"now {now.isoformat()} vs expires_at {signal.expires_at.isoformat()}",
    )

    # Entry window (session, holidays, early close)
    in_window, why = entry_window_open(now, r.entry_window, r.early_close_entry_end)
    rule("entry_window", in_window, why)

    # Stop required and below entry
    entry_px = signal.entry.price
    stop_ok = signal.stop is not None and 0 < signal.stop < entry_px
    rule(
        "stop_required",
        stop_ok,
        f"stop {signal.stop} vs entry {entry_px}" if signal.stop is not None else "no stop",
    )

    # Min reward:risk (target required for the bracket)
    if stop_ok and signal.target is not None and signal.stop is not None:
        rr = (signal.target - entry_px) / (entry_px - signal.stop)
        rule(
            "min_reward_risk",
            rr >= r.min_reward_risk,
            f"reward:risk {rr:.2f} vs min {r.min_reward_risk:g}",
        )
    else:
        rule(
            "min_reward_risk",
            False,
            "no target" if signal.target is None else "cannot compute without a valid stop",
        )

    # Price sanity: fresh, live quote within tolerance of the entry
    q = state.quote
    if q is None:
        rule("price_sanity", False, "no quote")
    else:
        age = (now - q.ts).total_seconds()
        ref = q.last if q.last > 0 else q.mid
        dev = abs(entry_px - ref) / ref * 100 if ref > 0 else float("inf")
        if q.delayed:
            rule("price_sanity", False, "quote is delayed, not live")
        elif age > r.quote_max_age_s:
            rule("price_sanity", False, f"quote is {age:.0f}s old (max {r.quote_max_age_s:g}s)")
        else:
            rule(
                "price_sanity",
                dev <= r.price_tolerance_pct,
                f"entry {entry_px} is {dev:.2f}% from last {ref} (max {r.price_tolerance_pct:g}%)",
            )

    # Duplicate: same symbol already open or entered today
    sym = signal.symbol.upper()
    open_here = _held(state.positions, sym) != 0
    entered_today = any(_is_entry_order(o) and o.symbol.upper() == sym for o in state.orders_today)
    rule(
        "duplicate",
        not (open_here or entered_today),
        "position already open"
        if open_here
        else "already entered today"
        if entered_today
        else "no open or prior entry",
    )

    # Sizing and the per-trade caps
    sizing = size(signal, state, cfg)
    rule(
        "max_loss_per_trade",
        sizing.qty * sizing.risk_per_share <= r.max_loss_per_trade_usd + 1e-9 and sizing.qty > 0,
        f"qty {sizing.qty} x risk/share {sizing.risk_per_share:.2f} = "
        f"{sizing.risk_usd_used:.2f} (max {r.max_loss_per_trade_usd:g})",
    )
    notional = sizing.qty * sizing.effective_entry
    rule(
        "max_order_value",
        sizing.qty > 0 and notional <= r.max_order_usd + 1e-9,
        f"order value {notional:.2f} (max {r.max_order_usd:g})"
        + ("; capped" if "max_order_usd" in sizing.caps else ""),
    )
    max_pos = r.max_position_pct / 100 * acct.net_liquidation
    rule(
        "max_position",
        sizing.qty > 0 and notional <= max_pos + 1e-9,
        f"position {notional:.2f} vs {r.max_position_pct:g}% of equity {max_pos:.2f}"
        + ("; capped" if "max_position_pct" in sizing.caps else ""),
    )
    rule(
        "cash_buffer",
        sizing.qty > 0,
        f"available {acct.available_funds:.2f}, buffer {r.cash_buffer_pct:g}%"
        + ("; capped" if "cash_buffer" in sizing.caps else "")
        + ("; no cash for even one share" if sizing.qty == 0 else ""),
    )

    # Max trades per day (pending entries count)
    entries_today = sum(1 for o in state.orders_today if _is_entry_order(o))
    rule(
        "max_trades_per_day",
        entries_today < r.max_trades_per_day,
        f"{entries_today} entries today (max {r.max_trades_per_day})",
    )

    # Max open positions (pending entries for other symbols count)
    open_syms = {p.symbol.upper() for p in state.positions if p.qty != 0}
    pending_syms = {
        o.symbol.upper()
        for o in state.orders_today
        if _is_entry_order(o) and o.status.is_working and o.symbol.upper() not in open_syms
    }
    n_open = len(open_syms) + len(pending_syms)
    rule(
        "max_open_positions",
        n_open < r.max_open_positions,
        f"{len(open_syms)} open + {len(pending_syms)} pending (max {r.max_open_positions})",
    )

    # PDT guard: this entry will be a day trade (flat by the close)
    if r.pdt_guard and acct.net_liquidation < r.pdt_equity_threshold_usd:
        rule(
            "pdt_guard",
            state.day_trades_in_window < r.pdt_max_day_trades,
            f"{state.day_trades_in_window} day trades in last {r.pdt_window_business_days} "
            f"business days (max {r.pdt_max_day_trades}) with equity "
            f"{acct.net_liquidation:.0f} < {r.pdt_equity_threshold_usd:g}",
        )
    else:
        rule("pdt_guard", True, "off" if not r.pdt_guard else "equity above threshold")

    accepted = all(x.passed for x in results)
    return Decision(
        accepted=accepted,
        qty=sizing.qty if accepted else 0,
        reasons=tuple(
            x.detail if x.rule in ("entry_window", "price_sanity") else f"{x.rule}: {x.detail}"
            for x in results
            if not x.passed
        ),
        rule_results=tuple(results),
        is_exit=False,
    )
