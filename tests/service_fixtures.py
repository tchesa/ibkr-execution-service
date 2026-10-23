"""Shared helpers: a Service wired to the FakeBroker, a FixedClock and an in-memory DB."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from ibkr_exec.alerts.slack import RecordingAlerter
from ibkr_exec.api.app import create_app
from ibkr_exec.api.auth import sign
from ibkr_exec.broker.fake import FakeBroker
from ibkr_exec.clock import FixedClock
from ibkr_exec.config import Config
from ibkr_exec.db.repo import Repository
from ibkr_exec.service import Service
from tests.conftest import T0

SECRETS = {"bot": "bot-secret", "admin": "admin-secret"}


async def make_repo() -> Repository:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    repo = Repository(engine)
    await repo.create_all()
    return repo


@dataclass
class Harness:
    svc: Service
    broker: FakeBroker
    clock: FixedClock
    alerts: RecordingAlerter
    repo: Repository
    cfg: Config


async def make_harness(
    cfg: Config | None = None,
    *,
    cash: float = 10_000.0,
    account_id: str = "DU1234567",
    env: dict[str, str] | None = None,
    start: bool = True,
    quotes: dict[str, float] | None = None,
) -> Harness:
    cfg = cfg or Config()
    clock = FixedClock(T0)
    broker = FakeBroker(clock=clock, cash=cash, account_id=account_id)
    for sym, px in (quotes or {"TSLA": 100.0, "SPY": 500.0}).items():
        broker.set_quote(sym, px)
    repo = await make_repo()
    alerts = RecordingAlerter(repo, clock)
    svc = Service(cfg, broker, repo, clock, alerts, env=env or {})
    if start:
        await svc.start()
    return Harness(svc, broker, clock, alerts, repo, cfg)


def signal_payload(**over: Any) -> dict[str, Any]:
    p = {
        "signal_id": "orb-TSLA-2026-10-23-1015",
        "symbol": "TSLA",
        "side": "buy",
        "entry": {"type": "limit", "price": 100.0},
        "stop": 99.0,
        "target": 102.0,
        "risk_usd": 50,
        "expires_at": (T0 + timedelta(minutes=5)).isoformat(),
        "strategy": "orb-v1",
        "reason": "test",
    }
    p.update(over)
    return p


class Client:
    """httpx client that signs every request with the chosen key."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.app = create_app(h.svc, SECRETS)
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def close(self) -> None:
        await self._client.aclose()

    def headers(self, key: str, method: str, path: str, body: bytes, ts: float | None = None):
        ts_s = str(int(self.h.clock.now().timestamp() if ts is None else ts))
        return {
            "x-key-id": key,
            "x-timestamp": ts_s,
            "x-signature": sign(SECRETS[key], method, path, ts_s, body),
            "content-type": "application/json",
        }

    async def request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        key: str = "bot",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        body = json.dumps(json_body).encode() if json_body is not None else b""
        hdrs = headers if headers is not None else self.headers(key, method, path, body)
        return await self._client.request(method, path, content=body, headers=hdrs)

    async def post(self, path: str, json_body: Any = None, key: str = "bot") -> httpx.Response:
        return await self.request("POST", path, json_body, key)

    async def get(self, path: str, key: str = "bot") -> httpx.Response:
        return await self.request("GET", path, None, key)
