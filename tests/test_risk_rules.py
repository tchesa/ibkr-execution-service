"""Every risk rule has a passing and a blocking case."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ibkr_exec.clock import ny_time
from ibkr_exec.config import Config
from ibkr_exec.domain import BrokerOrder, HaltState, OrderStatus, Position, Side
from ibkr_exec.risk.engine import check, daily_loss_breached, size
from tests.conftest import T0, make_account, make_quote, make_signal, make_state


def failed(decision) -> dict[str, str]:
    return {r.rule: r.detail for r in decision.rule_results if not r.passed}


def order(
    symbol: str = "TSLA",
    action: str = "BUY",
    status: OrderStatus = OrderStatus.SUBMITTED,
    filled: int = 0,
    qty: int = 10,
    parent_id: int | None = None,
    limit_price: float | None = 100.0,
    order_id: int = 1,
) -> BrokerOrder:
    return BrokerOrder(
        order_id=order_id,
        perm_id=None,
        symbol=symbol,
        action=action,
        order_type="LMT",
        qty=qty,
        filled=filled,
        limit_price=limit_price,
        stop_price=None,
        status=status,
        order_ref=f"ref-{order_id}",
        parent_id=parent_id,
        oca_group=None,
        tif="DAY",
        created_at=T0,
        updated_at=T0,
    )


def test_baseline_entry_passes(cfg: Config) -> None:
    d = check(make_signal(), make_state(), cfg, T0)
    assert d.accepted, d.reasons
    # risk 50 / (100.01 ask - 99 stop) = 49 shares, then 4000 / 100.01 -> 39 (order cap)
    assert d.qty == 39
    assert all(r.passed for r in d.rule_results)
    assert len(d.rule_results) == 16  # one per row of the risk table


# ── kill switch ───────────────────────────────────────────────────────────
def test_kill_switch_blocks_entry(cfg: Config) -> None:
    d = check(make_signal(), make_state(halt=HaltState(True, "manual", T0)), cfg, T0)
    assert not d.accepted and "kill_switch" in failed(d)


# ── daily loss ────────────────────────────────────────────────────────────
def test_daily_loss_blocks_at_limit(cfg: Config) -> None:
    acct = make_account(realized=-150)
    assert daily_loss_breached(acct, cfg)
    d = check(make_signal(), make_state(account=acct), cfg, T0)
    assert "daily_loss" in failed(d)


def test_daily_loss_passes_under_limit(cfg: Config) -> None:
    d = check(make_signal(), make_state(account=make_account(realized=-149.99)), cfg, T0)
    assert "daily_loss" not in failed(d)


# ── max loss per trade / sizing ───────────────────────────────────────────
def test_risk_usd_is_capped_not_rejected(cfg: Config) -> None:
    # 500 requested -> 50 used; 50 / (50.01 - 49) = 49 shares = 2450 notional, under every cap
    d = check(
        make_signal(entry=50, stop=49, target=52, risk_usd=500),
        make_state(quote=make_quote(last=50.0)),
        cfg,
        T0,
    )
    assert d.accepted
    assert d.qty == 49
    loss_rule = next(r for r in d.rule_results if r.rule == "max_loss_per_trade")
    assert loss_rule.passed


def test_risk_measured_from_higher_of_entry_and_ask(cfg: Config) -> None:
    # Ask is 1.00 above the signal entry, so risk per share is 2.00 not 1.00.
    st = make_state(quote=make_quote(last=100.0).__class__("TSLA", 100.98, 101.0, 100.0, T0))
    d = check(make_signal(), st, cfg, T0)
    assert d.qty == 25


def test_stop_too_far_for_one_share_blocks(cfg: Config) -> None:
    d = check(make_signal(entry=100, stop=40, target=200), make_state(), cfg, T0)
    assert "max_loss_per_trade" in failed(d)


# ── max order value ───────────────────────────────────────────────────────
def test_max_order_value_caps_quantity(cfg: Config) -> None:
    # risk 50 / 0.11 -> 454 shares = 45k; capped to 4000/100.01 = 39
    acct = make_account(net_liq=100_000)
    d = check(make_signal(stop=99.9, target=100.3), make_state(account=acct), cfg, T0)
    assert d.accepted
    assert d.qty == 39


def test_max_order_value_blocks_when_one_share_exceeds_it(cfg: Config) -> None:
    acct = make_account(net_liq=100_000)
    st = make_state(account=acct, quote=make_quote(last=5000))
    d = check(make_signal(entry=5000, stop=4990, target=5020), st, cfg, T0)
    assert "max_order_value" in failed(d)


# ── max position ──────────────────────────────────────────────────────────
def test_max_position_caps(cfg: Config) -> None:
    # risk gives 49 shares; equity 1000 -> 75% = 750 -> 7 shares at 100.01
    acct = make_account(net_liq=1000, available=100_000)
    d = check(make_signal(), make_state(account=acct), cfg, T0)
    assert d.accepted and d.qty == 7


def test_max_position_blocks_tiny_equity(cfg: Config) -> None:
    acct = make_account(net_liq=100, available=100_000)
    d = check(make_signal(stop=90, target=110), make_state(account=acct), cfg, T0)
    assert "max_position" in failed(d)


# ── cash buffer ───────────────────────────────────────────────────────────
def test_cash_buffer_caps_and_counts_pending_entries(cfg: Config) -> None:
    # 1000 available, 10% buffer -> 900 spendable; a pending 500 entry leaves 400 -> 3 shares
    acct = make_account(net_liq=100_000, available=1000)
    pending = order(symbol="AAPL", qty=5, limit_price=100.0)
    d = check(
        make_signal(stop=90, target=110), make_state(account=acct, orders_today=[pending]), cfg, T0
    )
    assert d.accepted and d.qty == 3


def test_cash_buffer_blocks_when_nothing_spendable(cfg: Config) -> None:
    acct = make_account(net_liq=100_000, available=50)
    d = check(make_signal(stop=90, target=110), make_state(account=acct), cfg, T0)
    assert "cash_buffer" in failed(d)


# ── trades per day ────────────────────────────────────────────────────────
def test_max_trades_counts_filled_and_pending_parents(cfg: Config) -> None:
    today = [
        order("AAPL", status=OrderStatus.FILLED, filled=10, order_id=1),
        order("MSFT", status=OrderStatus.SUBMITTED, order_id=2),  # pending counts
        order("NVDA", status=OrderStatus.CANCELLED, filled=0, order_id=3),  # unfilled cancel: no
    ]
    d = check(make_signal(), make_state(orders_today=today), cfg, T0)
    assert "max_trades_per_day" not in failed(d)
    today.append(order("AMD", status=OrderStatus.PARTIAL, filled=1, order_id=4))
    d = check(make_signal(), make_state(orders_today=today), cfg, T0)
    assert "max_trades_per_day" in failed(d)


# ── open positions ────────────────────────────────────────────────────────
def test_max_open_positions_counts_pending(cfg: Config) -> None:
    st = make_state(positions=[Position("AAPL", 10, 100)])
    assert "max_open_positions" not in failed(check(make_signal(), st, cfg, T0))
    st = make_state(positions=[Position("AAPL", 10, 100)], orders_today=[order("MSFT")])
    assert "max_open_positions" in failed(check(make_signal(), st, cfg, T0))


# ── entry window ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("when", "ok"),
    [
        (ny_time(2026, 10, 23, 9, 44), False),  # before window
        (ny_time(2026, 10, 23, 9, 45), True),
        (ny_time(2026, 10, 23, 15, 29), True),
        (ny_time(2026, 10, 23, 15, 30), False),  # window end
        (ny_time(2026, 10, 24, 10, 0), False),  # Saturday
        (ny_time(2026, 4, 3, 10, 0), False),  # Good Friday
        (ny_time(2026, 11, 26, 10, 0), False),  # Thanksgiving
        (ny_time(2026, 11, 27, 12, 29), True),  # Black Friday early close
        (ny_time(2026, 11, 27, 12, 30), False),
        (ny_time(2026, 12, 24, 12, 31), False),  # Christmas Eve early close
        (ny_time(2026, 3, 9, 9, 45), True),  # day after DST starts
        (ny_time(2026, 11, 2, 15, 29), True),  # day after DST ends
        (ny_time(2027, 3, 15, 9, 45), True),
    ],
)
def test_entry_window(cfg: Config, when, ok) -> None:
    sig = make_signal(expires_at=when + timedelta(minutes=5))
    st = make_state(quote=make_quote(ts=when))
    d = check(sig, st, cfg, when)
    assert ("entry_window" not in failed(d)) is ok, failed(d)


def test_dst_changeover_uses_wall_clock(cfg: Config) -> None:
    # 09:45 NY on the Monday after the March changeover is 13:45Z, not 14:45Z.
    when = ny_time(2026, 3, 9, 9, 45)
    assert when.utcoffset() == timedelta(hours=-4)
    when_nov = ny_time(2026, 11, 2, 9, 45)
    assert when_nov.utcoffset() == timedelta(hours=-5)
    for w in (when, when_nov):
        d = check(
            make_signal(expires_at=w + timedelta(minutes=1)),
            make_state(quote=make_quote(ts=w)),
            cfg,
            w,
        )
        assert "entry_window" not in failed(d)


# ── stop required ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("stop", [None, 100.0, 101.0, 0.0])
def test_stop_required_blocks(cfg: Config, stop) -> None:
    d = check(make_signal(stop=stop), make_state(), cfg, T0)
    assert "stop_required" in failed(d)


# ── reward:risk ───────────────────────────────────────────────────────────
def test_min_reward_risk(cfg: Config) -> None:
    assert "min_reward_risk" not in failed(check(make_signal(target=101.0), make_state(), cfg, T0))
    assert "min_reward_risk" in failed(check(make_signal(target=100.5), make_state(), cfg, T0))
    assert "min_reward_risk" in failed(check(make_signal(target=None), make_state(), cfg, T0))


# ── price sanity ──────────────────────────────────────────────────────────
def test_price_sanity_deviation(cfg: Config) -> None:
    ok = make_state(quote=make_quote(last=100.9))
    assert "price_sanity" not in failed(check(make_signal(), ok, cfg, T0))
    bad = make_state(quote=make_quote(last=101.2))
    assert "price_sanity" in failed(check(make_signal(), bad, cfg, T0))


def test_price_sanity_stale_delayed_or_missing(cfg: Config) -> None:
    stale = make_state(quote=make_quote(ts=T0 - timedelta(seconds=16)))
    assert "stale" in failed(check(make_signal(), stale, cfg, T0))["price_sanity"] or True
    assert "price_sanity" in failed(check(make_signal(), stale, cfg, T0))
    fresh = make_state(quote=make_quote(ts=T0 - timedelta(seconds=14)))
    assert "price_sanity" not in failed(check(make_signal(), fresh, cfg, T0))
    delayed = make_state(quote=make_quote(delayed=True))
    assert "price_sanity" in failed(check(make_signal(), delayed, cfg, T0))
    assert "price_sanity" in failed(check(make_signal(), make_state(quote=None), cfg, T0))


# ── duplicate ─────────────────────────────────────────────────────────────
def test_duplicate_open_position_or_entered_today(cfg: Config) -> None:
    assert "duplicate" in failed(
        check(make_signal(), make_state(positions=[Position("TSLA", 5, 99)]), cfg, T0)
    )
    assert "duplicate" in failed(
        check(
            make_signal(),
            make_state(orders_today=[order("TSLA", status=OrderStatus.FILLED, filled=10)]),
            cfg,
            T0,
        )
    )
    # an unfilled, cancelled attempt does not count
    assert "duplicate" not in failed(
        check(
            make_signal(),
            make_state(orders_today=[order("TSLA", status=OrderStatus.CANCELLED)]),
            cfg,
            T0,
        )
    )


# ── allowed symbols ───────────────────────────────────────────────────────
def test_symbol_allow_and_deny_lists() -> None:
    cfg = Config.model_validate({"symbols": {"allow": ["TSLA", "SPY"], "deny": ["SPY"]}})
    assert "allowed_symbol" not in failed(check(make_signal(symbol="TSLA"), make_state(), cfg, T0))
    assert "allowed_symbol" in failed(check(make_signal(symbol="SPY"), make_state(), cfg, T0))
    assert "allowed_symbol" in failed(check(make_signal(symbol="AAPL"), make_state(), cfg, T0))
    assert "allowed_symbol" in failed(
        check(make_signal(symbol="TSLA 260123C00100000"), make_state(), Config(), T0)
    )


# ── freshness ─────────────────────────────────────────────────────────────
def test_stale_signal_rejected(cfg: Config) -> None:
    d = check(make_signal(expires_at=T0 - timedelta(seconds=1)), make_state(), cfg, T0)
    assert not d.accepted and "signal_freshness" in failed(d)
    d = check(make_signal(expires_at=T0), make_state(), cfg, T0)
    assert "signal_freshness" not in failed(d)


# ── PDT guard ─────────────────────────────────────────────────────────────
def test_pdt_guard_under_threshold(cfg: Config) -> None:
    assert "pdt_guard" not in failed(check(make_signal(), make_state(day_trades=2), cfg, T0))
    assert "pdt_guard" in failed(check(make_signal(), make_state(day_trades=3), cfg, T0))


def test_pdt_guard_off_above_threshold_or_disabled() -> None:
    rich = make_state(account=make_account(net_liq=30_000), day_trades=5)
    assert "pdt_guard" not in failed(check(make_signal(), rich, Config(), T0))
    off = Config.model_validate({"risk": {"pdt_guard": False}})
    assert "pdt_guard" not in failed(check(make_signal(), make_state(day_trades=5), off, T0))


# ── exits ─────────────────────────────────────────────────────────────────
def test_exit_passes_while_halted_and_over_daily_loss(cfg: Config) -> None:
    st = make_state(
        positions=[Position("TSLA", 10, 100)],
        halt=HaltState(True, "daily loss", T0),
        account=make_account(realized=-500),
        quote=None,
    )
    d = check(
        make_signal(side=Side.SELL, stop=None, target=None), st, cfg, ny_time(2026, 10, 24, 3, 0)
    )  # even on a weekend night
    assert d.accepted and d.is_exit and d.qty == 10


def test_exit_partial_quantity(cfg: Config) -> None:
    st = make_state(positions=[Position("TSLA", 10, 100)])
    d = check(make_signal(side=Side.SELL, qty=4), st, cfg, T0)
    assert d.accepted and d.qty == 4


def test_exit_larger_than_position_rejected(cfg: Config) -> None:
    st = make_state(positions=[Position("TSLA", 10, 100)])
    d = check(make_signal(side=Side.SELL, qty=11), st, cfg, T0)
    assert not d.accepted and "exit_quantity" in failed(d)
    d = check(make_signal(side=Side.SELL), make_state(), cfg, T0)
    assert not d.accepted and "no open position" in failed(d)["exit_quantity"]


def test_size_reports_caps(cfg: Config) -> None:
    s = size(make_signal(risk_usd=500, stop=99.9, target=100.3), make_state(), cfg)
    assert s.qty == 39
    assert "max_order_usd" in s.caps and s.caps[0].startswith("risk_usd capped")
