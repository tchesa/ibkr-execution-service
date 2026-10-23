"""HTTP routes. Every trading endpoint is HMAC-authenticated; admin actions need the admin key."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request, Response

from ibkr_exec.api.schemas import DecisionOut, FlattenIn, HaltIn, SignalIn, SignalStatusOut
from ibkr_exec.broker.base import BrokerError
from ibkr_exec.service import Service

router = APIRouter()


def _svc(request: Request) -> Service:
    return request.app.state.service


def _require_ready(svc: Service) -> None:
    if not svc.ready:
        raise HTTPException(503, "service not ready: initial reconcile has not completed")
    if not svc.broker.is_connected():
        raise HTTPException(503, "gateway not connected")


@router.post("/signals", response_model=DecisionOut)
async def post_signal(request: Request, payload: SignalIn, response: Response) -> DecisionOut:
    svc = _svc(request)
    _require_ready(svc)
    out = await svc.submit_signal(payload)
    response.status_code = 200 if out.duplicate else (201 if out.decision == "accepted" else 200)
    return out


@router.get("/signals/{signal_id}", response_model=SignalStatusOut)
async def get_signal(request: Request, signal_id: str) -> SignalStatusOut:
    out = await _svc(request).get_signal(signal_id)
    if out is None:
        raise HTTPException(404, "unknown signal_id")
    return out


@router.get("/positions")
async def positions(request: Request) -> list[dict]:
    svc = _svc(request)
    _require_ready(svc)
    return [asdict(p) for p in await svc.broker.positions()]


@router.get("/orders")
async def orders(request: Request) -> dict:
    svc = _svc(request)
    _require_ready(svc)
    return {
        "open": [_order(o) for o in await svc.broker.open_orders()],
        "today": [_order(o) for o in await svc.broker.orders_today()],
    }


def _order(o) -> dict:
    d = asdict(o)
    d["status"] = o.status.value
    return d


@router.get("/account")
async def account(request: Request) -> dict:
    svc = _svc(request)
    _require_ready(svc)
    try:
        return asdict(await svc.broker.account_state())
    except BrokerError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/flatten")
async def flatten(request: Request, payload: FlattenIn | None = None) -> dict:
    svc = _svc(request)
    _require_ready(svc)
    payload = payload or FlattenIn()
    results = await svc.flattener.flatten(payload.symbol, f"manual flatten: {payload.reason}")
    for o in await svc.broker.open_orders():
        if (
            o.parent_id is None
            and o.action == "BUY"
            and o.status.is_working
            and (payload.symbol is None or o.symbol.upper() == payload.symbol.upper())
        ):
            await svc.broker.cancel(o.order_id)
    return {"results": [asdict(r) for r in results], "ok": all(r.ok for r in results)}


@router.post("/halt")
async def halt(request: Request, payload: HaltIn | None = None) -> dict:
    payload = payload or HaltIn()
    st = await _svc(request).set_halt(payload.reason, "api")
    return asdict(st)


@router.post("/resume")
async def resume(request: Request) -> dict:
    st = await _svc(request).resume("api")
    return asdict(st)


@router.get("/health")
async def health(request: Request, response: Response) -> dict:
    svc = _svc(request)
    h = await svc.health()
    if not h["ready"] or not h["gateway_connected"]:
        response.status_code = 503
    return h
