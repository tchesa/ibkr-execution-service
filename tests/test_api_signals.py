from __future__ import annotations

from datetime import timedelta

import pytest

from ibkr_exec.domain import OrderStatus
from tests.conftest import T0
from tests.service_fixtures import Client, Harness, make_harness, signal_payload


@pytest.fixture
async def h() -> Harness:
    return await make_harness()


@pytest.fixture
async def client(h: Harness):
    c = Client(h)
    yield c
    await c.close()


async def test_accepted_entry_places_one_bracket(h: Harness, client: Client) -> None:
    r = await client.post("/signals", signal_payload())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["decision"] == "accepted" and out["qty"] == 39 and out["order_id"]
    orders = h.broker.orders_with_ref("orb-TSLA-2026-10-23-1015")
    assert len(orders) == 3
    parent = next(o for o in orders if o.parent_id is None)
    stop = next(o for o in orders if o.order_type == "STP")
    target = next(o for o in orders if o.order_type == "LMT" and o.parent_id)
    assert parent.tif == "DAY" and stop.tif == "GTC" and target.tif == "GTC"
    assert stop.stop_price == 99.0 and target.limit_price == 102.0
    assert stop.oca_group == target.oca_group == "orb-TSLA-2026-10-23-1015"
    rows = await h.repo.orders_for_ref("orb-TSLA-2026-10-23-1015")
    assert sorted(r.role for r in rows) == ["parent", "stop", "target"]


async def test_duplicate_signal_id_returns_first_decision_and_places_nothing(
    h: Harness, client: Client
) -> None:
    first = (await client.post("/signals", signal_payload())).json()
    again = await client.post("/signals", signal_payload(entry={"type": "limit", "price": 100.5}))
    assert again.status_code == 200
    out = again.json()
    assert out["duplicate"] is True
    assert out["order_id"] == first["order_id"] and out["qty"] == first["qty"]
    assert len(h.broker.placed) == 1


async def test_duplicate_of_rejected_signal_stays_rejected(h: Harness, client: Client) -> None:
    bad = signal_payload(stop=None)
    r1 = (await client.post("/signals", bad)).json()
    assert r1["decision"] == "rejected"
    r2 = (await client.post("/signals", signal_payload())).json()  # same id, now "valid"
    assert r2["decision"] == "rejected" and r2["duplicate"] is True
    assert h.broker.placed == []


async def test_order_already_at_broker_is_not_placed_twice(h: Harness, client: Client) -> None:
    """A crash between placement and DB write: the orderRef at the broker wins."""
    from ibkr_exec.domain import BracketRequest

    await h.broker.place_bracket(
        BracketRequest("orb-TSLA-2026-10-23-1015", "TSLA", 5, 100.0, 99.0, 102.0)
    )
    r = (await client.post("/signals", signal_payload())).json()
    assert r["decision"] == "accepted" and r["duplicate"] is True and r["qty"] == 5
    assert len(h.broker.placed) == 1


async def test_stale_signal_rejected(h: Harness, client: Client) -> None:
    r = await client.post(
        "/signals", signal_payload(expires_at=(T0 - timedelta(seconds=1)).isoformat())
    )
    assert r.status_code == 200
    out = r.json()
    assert out["decision"] == "rejected" and any("freshness" in x for x in out["reasons"])
    row = await h.repo.get_signal("orb-TSLA-2026-10-23-1015")
    assert row is not None and row.decision == "rejected"
    assert len(row.rule_results["rules"]) == 16


async def test_sell_larger_than_position_rejected(h: Harness, client: Client) -> None:
    h.broker.set_position("TSLA", 10, 99.0)
    r = (await client.post("/signals", signal_payload(signal_id="x1", side="sell", qty=11))).json()
    assert r["decision"] == "rejected"
    r = (await client.post("/signals", signal_payload(signal_id="x2", side="sell", qty=4))).json()
    assert r["decision"] == "accepted" and r["qty"] == 4
    sell = h.broker.order(r["order_id"])
    assert sell.action == "SELL" and sell.qty == 4 and sell.order_ref == "x2"


async def test_exit_passes_while_halted(h: Harness, client: Client) -> None:
    h.broker.set_position("TSLA", 10, 99.0)
    await h.svc.set_halt("test", "test")
    entry = (await client.post("/signals", signal_payload(signal_id="e1"))).json()
    assert entry["decision"] == "rejected"
    exit_ = (await client.post("/signals", signal_payload(signal_id="e2", side="sell"))).json()
    assert exit_["decision"] == "accepted" and exit_["qty"] == 10


async def test_partial_fill_resizes_children(h: Harness, client: Client) -> None:
    out = (await client.post("/signals", signal_payload())).json()
    parent_id = out["order_id"]
    await h.broker.fill(parent_id, qty=10)
    children = [o for o in h.broker.orders_with_ref(out["signal_id"]) if o.parent_id == parent_id]
    assert {c.qty for c in children} == {10}
    await h.broker.fill(parent_id, qty=29)  # the rest
    children = [o for o in h.broker.orders_with_ref(out["signal_id"]) if o.parent_id == parent_id]
    assert {c.qty for c in children} == {39}
    assert h.broker.order(parent_id).status is OrderStatus.FILLED


async def test_stale_parent_cancelled_after_timeout_keeps_filled_part_protected(
    h: Harness, client: Client
) -> None:
    out = (await client.post("/signals", signal_payload())).json()
    pid = out["order_id"]
    await h.broker.fill(pid, qty=7)
    h.clock.advance(minutes=5)
    cancelled = await h.svc.bracket.cancel_stale_parents()
    assert cancelled == [pid]
    assert h.broker.order(pid).status is OrderStatus.CANCELLED
    children = [o for o in h.broker.orders_with_ref(out["signal_id"]) if o.parent_id == pid]
    assert all(c.status.is_working and c.qty == 7 for c in children)


async def test_unlisted_warning_cancels_order_and_alerts(h: Harness, client: Client) -> None:
    out = (await client.post("/signals", signal_payload())).json()
    await h.broker.emit_warning(
        399, "Order held: price exceeds precautionary limit", out["order_id"]
    )
    assert h.broker.order(out["order_id"]).status is OrderStatus.CANCELLED
    assert "Order held by broker warning" in h.alerts.titles()
    before = len(h.alerts.sent)
    await h.broker.emit_warning(2104, "Market data farm connection is OK", None)
    assert len(h.alerts.sent) == before


async def test_broker_rejection_is_recorded_and_alerted(h: Harness, client: Client) -> None:
    h.broker.reject_next_reason = "precautionary constraint"
    r = (await client.post("/signals", signal_payload())).json()
    assert r["decision"] == "rejected" and "broker" in r["reasons"][0]
    assert "Entry placement failed" in h.alerts.titles()
    row = await h.repo.get_signal(r["signal_id"])
    assert row.decision == "rejected" and row.error


async def test_get_signal_status(h: Harness, client: Client) -> None:
    out = (await client.post("/signals", signal_payload())).json()
    r = await client.get(f"/signals/{out['signal_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "accepted" and len(body["orders"]) == 3
    assert len(body["rule_results"]) == 16
    assert (await client.get("/signals/nope")).status_code == 404


async def test_auth_failures(h: Harness, client: Client) -> None:
    body = signal_payload()
    r = await client.request("POST", "/signals", body, headers={"content-type": "application/json"})
    assert r.status_code == 401
    # auth runs before body validation: garbage without a signature is 401, not 422
    r = await client.request("POST", "/signals", {"nope": 1}, headers={})
    assert r.status_code == 401
    hdrs = client.headers("bot", "POST", "/signals", b"{}")
    r = await client.request("POST", "/signals", body, headers=hdrs)  # body mismatch
    assert r.status_code == 401
    old = client.headers("admin", "POST", "/halt", b"", ts=h.clock.now().timestamp() - 31)
    r = await client.request("POST", "/halt", None, headers=old)
    assert r.status_code == 401
    # replay
    hdrs = client.headers("admin", "POST", "/halt", b"")
    assert (await client.request("POST", "/halt", None, headers=hdrs)).status_code == 200
    assert (await client.request("POST", "/halt", None, headers=hdrs)).status_code == 401


async def test_admin_endpoints_need_admin_key(h: Harness, client: Client) -> None:
    assert (await client.post("/halt", key="bot")).status_code == 403
    assert (await client.post("/halt", key="admin")).status_code == 200
    assert h.svc.halt.halted
    assert (await client.post("/resume", key="bot")).status_code == 403
    assert (await client.post("/resume", key="admin")).status_code == 200
    assert not h.svc.halt.halted
    assert (await client.post("/flatten", key="bot")).status_code == 403


async def test_rate_limit(h: Harness, client: Client) -> None:
    codes = []
    for i in range(11):
        r = await client.post("/signals", signal_payload(signal_id=f"rl-{i}", stop=None))
        codes.append(r.status_code)
    assert codes[:10] == [200] * 10 and codes[10] == 429


async def test_not_ready_returns_503() -> None:
    h = await make_harness(start=False)
    c = Client(h)
    try:
        r = await c.post("/signals", signal_payload())
        assert r.status_code == 503
        r = await c.get("/health")
        assert r.status_code == 503 and r.json()["ready"] is False
        await h.svc.start()
        r = await c.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] and body["gateway_connected"] and body["last_reconcile_at"]
    finally:
        await c.close()


async def test_read_endpoints(h: Harness, client: Client) -> None:
    h.broker.set_position("AAPL", 3, 99.0)
    assert (await client.get("/positions")).json()[0]["qty"] == 3
    acct = (await client.get("/account")).json()
    assert acct["account_id"] == "DU1234567"
    await client.post("/signals", signal_payload())
    orders = (await client.get("/orders")).json()
    assert len(orders["open"]) == 3 and len(orders["today"]) == 3


async def test_flatten_endpoint_closes_all(h: Harness, client: Client) -> None:
    out = (await client.post("/signals", signal_payload())).json()
    await h.broker.fill(out["order_id"])
    r = await client.post("/flatten", {"reason": "test"}, key="admin")
    assert r.status_code == 200 and r.json()["ok"]
    res = r.json()["results"][0]
    assert res["qty"] == 39 and len(res["cancelled"]) == 2
    sell = h.broker.order(res["sell_order_id"])
    assert sell.action == "SELL" and sell.qty == 39


async def test_invalid_payloads_rejected(h: Harness, client: Client) -> None:
    r = await client.post("/signals", signal_payload(expires_at="2026-10-23T10:20:00"))  # naive
    assert r.status_code == 422
    r = await client.post("/signals", signal_payload(symbol="TSLA 260123C00100000"))
    assert r.status_code == 422
    r = await client.post("/signals", {**signal_payload(), "outside_rth": True})
    assert r.status_code == 422
