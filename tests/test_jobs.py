from __future__ import annotations

import pytest

from ibkr_exec.clock import ny_time
from ibkr_exec.config import Config
from ibkr_exec.domain import BracketRequest, OrderRequest, OrderStatus
from ibkr_exec.service import ModeMismatch, Service
from tests.service_fixtures import Harness, make_harness


@pytest.fixture
async def h() -> Harness:
    return await make_harness()


# ── reconcile ─────────────────────────────────────────────────────────────
async def test_reconcile_detects_unknown_position_and_places_stop(h: Harness) -> None:
    h.broker.set_quote("AAPL", 50.0)
    h.broker.set_position("AAPL", 4, 49.0)
    report = await h.svc.reconciler.run("test")
    assert report.unknown_positions == ("AAPL",)
    assert report.stops_placed and report.stops_placed[0].startswith("AAPL")
    stops = [o for o in await h.broker.open_orders() if o.order_type == "STP"]
    assert len(stops) == 1 and stops[0].qty == 4 and stops[0].stop_price == 49.5  # last - 1%
    titles = h.alerts.titles()
    assert "Reconcile drift: unknown_position" in titles
    assert "Reconcile drift: missing_stop" in titles
    # a second run does not re-alert or re-place
    n = len(h.alerts.sent)
    report = await h.svc.reconciler.run("test")
    assert report.stops_placed == () and len(h.alerts.sent) == n


async def test_reconcile_missing_stop_uses_signal_stop(h: Harness) -> None:
    from tests.service_fixtures import Client, signal_payload

    c = Client(h)
    try:
        out = (await c.post("/signals", signal_payload())).json()
    finally:
        await c.close()
    await h.broker.fill(out["order_id"])
    stop = next(o for o in h.broker.orders_with_ref(out["signal_id"]) if o.order_type == "STP")
    await h.broker.cancel(stop.order_id)  # someone cancelled the stop by hand
    report = await h.svc.reconciler.run("test")
    assert report.stops_placed
    new_stop = [o for o in await h.broker.open_orders() if o.order_type == "STP"]
    assert len(new_stop) == 1 and new_stop[0].stop_price == 99.0 and new_stop[0].qty == 39
    rows = await h.repo.orders_for_ref(new_stop[0].order_ref)
    assert rows and rows[0].role == "protective_stop"


async def test_reconcile_detects_unknown_order(h: Harness) -> None:
    oid = h.broker.add_external_order(OrderRequest("manual", "SPY", "BUY", "LMT", 1, 400.0))
    report = await h.svc.reconciler.run("test")
    assert report.unknown_orders == (oid,)
    assert "Reconcile drift: unknown_order" in h.alerts.titles()
    assert (await h.repo.get_order(oid)) is not None


async def test_reconcile_ingests_fills_once(h: Harness) -> None:
    ids = await h.broker.place_bracket(BracketRequest("s", "TSLA", 5, 100.0, 99.0, 102.0))
    await h.broker.fill(ids.parent_id)  # callback stores the fill
    report = await h.svc.reconciler.run("test")
    assert report.new_fills == 0
    fills = await h.repo.fills_between(ny_time(2026, 10, 23).date(), ny_time(2026, 10, 23).date())
    assert len(fills) == 1


# ── daily loss / halt ─────────────────────────────────────────────────────
async def test_daily_loss_trips_halt_and_survives_restart(h: Harness) -> None:
    ids = await h.broker.place_bracket(BracketRequest("s", "TSLA", 39, 100.0, 99.0, 102.0))
    await h.broker.fill(ids.parent_id)
    await h.broker.fill(ids.stop_id, price=96.0)  # -4 x 39 = -156 (plus commission)
    assert h.svc.halt.halted and "daily realized loss" in (h.svc.halt.reason or "")
    assert "HALT tripped" in h.alerts.titles()
    # A new Service on the same database starts halted.
    svc2 = Service(h.cfg, h.broker, h.repo, h.clock, h.alerts, env={})
    await svc2.start()
    assert svc2.halt.halted
    # Only /resume lifts it.
    await svc2.resume("api")
    assert not svc2.halt.halted and not (await h.repo.get_halt()).halted


# ── startup mode check ────────────────────────────────────────────────────
async def test_startup_fails_when_mode_and_account_disagree() -> None:
    with pytest.raises(ModeMismatch):
        await make_harness(Config(), account_id="U7654321")
    live = Config.model_validate({"mode": "live"})
    with pytest.raises(ModeMismatch):
        await make_harness(live, account_id="DU1234567", env={"CONFIRM_LIVE": "yes"})
    with pytest.raises(ModeMismatch):
        await make_harness(live, account_id="U7654321", env={})
    h = await make_harness(live, account_id="U7654321", env={"CONFIRM_LIVE": "yes"})
    assert h.svc.ready


# ── keepalive / disconnect ────────────────────────────────────────────────
async def test_disconnect_alerts_after_two_minutes_and_reconciles_on_recovery(h: Harness) -> None:
    await h.broker.drop_connection()
    await h.svc.job_keepalive()  # reconnects immediately (fake connect succeeds)
    assert h.broker.is_connected()
    assert "Gateway session lost" not in h.alerts.titles()

    # Now make reconnects fail for a while.
    async def failing_connect():
        raise ConnectionError("gateway down")

    real_connect = h.broker.connect
    h.broker.connect = failing_connect
    await h.broker.drop_connection()
    await h.svc.job_keepalive()
    h.clock.advance(seconds=61)
    await h.svc.job_keepalive()
    assert "Gateway session lost" not in h.alerts.titles()
    h.clock.advance(seconds=61)
    await h.svc.job_keepalive()
    assert "Gateway session lost" in h.alerts.titles()
    assert h.alerts.titles().count("Gateway session lost") == 1

    h.broker.connect = real_connect
    h.clock.advance(seconds=600)
    before = h.svc.last_reconcile_at
    await h.svc.job_keepalive()
    assert h.broker.is_connected()
    assert "Gateway reconnected" in h.alerts.titles()
    assert h.svc.last_reconcile_at != before


# ── end of day ────────────────────────────────────────────────────────────
async def test_eod_alerts_on_leftover_position_and_writes_pnl(h: Harness) -> None:
    ids = await h.broker.place_bracket(BracketRequest("s", "TSLA", 5, 100.0, 99.0, 102.0))
    await h.broker.fill(ids.parent_id)
    await h.svc.job_eod(force=True)
    assert "Positions remaining after the close" in h.alerts.titles()
    row = await h.repo.get_daily_pnl(ny_time(2026, 10, 23).date())
    assert row is not None and row.trades == 1 and row.commissions == 1.0
    assert any(t.startswith("Daily summary") for t in h.alerts.titles())


async def test_eod_clean_day(h: Harness) -> None:
    await h.svc.job_eod(force=True)
    assert "Positions remaining after the close" not in h.alerts.titles()


async def test_eod_does_not_run_before_close_plus_ten(h: Harness) -> None:
    h.clock.set(ny_time(2026, 10, 23, 13, 10))
    await h.svc.job_eod()
    assert not any(t.startswith("Daily summary") for t in h.alerts.titles())
    h.clock.set(ny_time(2026, 10, 23, 16, 10))
    await h.svc.job_eod()
    assert any(t.startswith("Daily summary") for t in h.alerts.titles())
    n = len(h.alerts.sent)
    await h.svc.job_eod()  # idempotent within the day
    assert len(h.alerts.sent) == n


# ── pre-open ──────────────────────────────────────────────────────────────
async def test_preopen_passes_and_fails(h: Harness) -> None:
    res = await h.svc.job_preopen(force=True)
    assert res["ok"], res
    h.broker.set_quote("SPY", 500.0, delayed=True)
    res = await h.svc.job_preopen(force=True)
    assert not res["ok"] and "delayed" in res["problems"][0]
    assert "Pre-open check failed" in h.alerts.titles()


async def test_preopen_flags_halt(h: Harness) -> None:
    await h.svc.set_halt("yesterday's loss", "test")
    res = await h.svc.job_preopen(force=True)
    assert not res["ok"] and any("halt" in p for p in res["problems"])


# ── stale entries at window end ───────────────────────────────────────────
async def test_unfilled_parent_cancelled_at_entry_window_end(h: Harness) -> None:
    h.clock.set(ny_time(2026, 10, 23, 15, 28))
    ids = await h.broker.place_bracket(BracketRequest("s", "TSLA", 5, 100.0, 99.0, 102.0))
    h.clock.set(ny_time(2026, 10, 23, 15, 30))
    assert await h.svc.bracket.cancel_stale_parents() == [ids.parent_id]
    assert h.broker.order(ids.parent_id).status is OrderStatus.CANCELLED
