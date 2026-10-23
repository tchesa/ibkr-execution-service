from __future__ import annotations

import pytest

from ibkr_exec.domain import BracketRequest, OrderStatus
from tests.service_fixtures import Harness, make_harness


@pytest.fixture
async def h() -> Harness:
    return await make_harness()


async def entered(h: Harness, qty: int = 10) -> tuple[int, int, int]:
    ids = await h.broker.place_bracket(BracketRequest("sig-1", "TSLA", qty, 100.0, 99.0, 102.0))
    await h.svc.bracket.handle_status(h.broker.order(ids.parent_id))
    await h.broker.fill(ids.parent_id)
    return ids.parent_id, ids.stop_id, ids.target_id


async def test_flatten_cancels_children_before_selling(h: Harness) -> None:
    _, stop_id, target_id = await entered(h)
    events: list[str] = []
    orig_cancel, orig_place = h.broker.cancel, h.broker.place_order

    async def cancel(oid):
        events.append(f"cancel:{oid}")
        await orig_cancel(oid)

    async def place(req):
        events.append(f"place:{req.action}")
        return await orig_place(req)

    h.broker.cancel, h.broker.place_order = cancel, place
    results = await h.svc.flattener.flatten(None, "test")
    assert len(results) == 1 and results[0].ok
    assert events == [f"cancel:{stop_id}", f"cancel:{target_id}", "place:SELL"]
    sell = h.broker.order(results[0].sell_order_id)
    assert sell.order_type == "LMT" and sell.limit_price < 100.0  # marketable, below the bid
    await h.broker.fill(sell.order_id)
    assert await h.broker.positions() == []


async def test_failed_sell_replaces_stop_and_alerts(h: Harness) -> None:
    _, stop_id, _ = await entered(h)
    h.broker.fail_next_place = True
    results = await h.svc.flattener.flatten(None, "test")
    res = results[0]
    assert not res.ok and res.protective_stop_id is not None
    stop = h.broker.order(res.protective_stop_id)
    assert stop.order_type == "STP" and stop.stop_price == 99.0 and stop.qty == 10
    assert stop.tif == "GTC" and stop.status.is_working
    assert "Flatten failed, protective stop re-placed" in h.alerts.titles()
    assert h.broker.order(stop_id).status is OrderStatus.CANCELLED


async def test_failed_cancel_aborts_and_alerts(h: Harness) -> None:
    await entered(h)
    h.broker.fail_next_cancel = True
    results = await h.svc.flattener.flatten(None, "test")
    assert not results[0].ok and results[0].sell_order_id is None
    assert "Flatten: could not cancel child" in h.alerts.titles()
    # the original stop is still working: never left unprotected
    stops = [o for o in await h.broker.open_orders() if o.order_type == "STP"]
    assert len(stops) == 1


async def test_flatten_one_symbol_only(h: Harness) -> None:
    await entered(h)
    h.broker.set_quote("AAPL", 50.0)
    h.broker.set_position("AAPL", 4, 50.0)
    results = await h.svc.flattener.flatten("AAPL", "test")
    assert [r.symbol for r in results] == ["AAPL"]
    assert len([o for o in await h.broker.open_orders() if o.symbol == "TSLA"]) == 2


async def test_scheduled_flatten_cancels_unfilled_entries_too(h: Harness) -> None:
    await entered(h)
    ids = await h.broker.place_bracket(BracketRequest("sig-2", "SPY", 2, 500.0, 495.0, 510.0))
    await h.svc.job_flatten(force=True)
    assert h.broker.order(ids.parent_id).status is OrderStatus.CANCELLED
    working = [o for o in await h.broker.open_orders()]
    assert all(o.action == "SELL" and o.order_type == "LMT" for o in working)  # only the sell


async def test_flatten_job_time_on_normal_and_early_close_days(h: Harness) -> None:
    from datetime import date, time

    assert h.svc.flatten_time(date(2026, 10, 23)) == time(15, 55)
    assert h.svc.flatten_time(date(2026, 11, 27)) == time(12, 55)
    assert h.svc.flatten_time(date(2026, 10, 24)) is None
