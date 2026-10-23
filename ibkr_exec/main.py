"""Entry point: build the service from config + environment and serve it.

BROKER=fake uv run python -m ibkr_exec.main      # in-memory broker, for local checks
BROKER=ib   uv run python -m ibkr_exec.main      # IB Gateway on IB_HOST:IB_PORT
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ibkr_exec.alerts.slack import SlackAlerter
from ibkr_exec.api.app import create_app
from ibkr_exec.broker.base import Broker
from ibkr_exec.broker.fake import FakeBroker
from ibkr_exec.clock import SystemClock
from ibkr_exec.config import Config, load_config
from ibkr_exec.db.repo import Repository, make_engine
from ibkr_exec.jobs.scheduler import build_scheduler
from ibkr_exec.logging import get_logger, setup_logging
from ibkr_exec.service import ModeMismatch, Service

log = get_logger(__name__)


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        log.error("missing_env", name=name)
        sys.exit(2)
    return v


def _make_broker(cfg: Config, clock: SystemClock) -> Broker:
    kind = os.environ.get("BROKER", "ib").lower()
    if kind == "fake":
        fb = FakeBroker(clock=clock, cash=float(os.environ.get("FAKE_CASH", "10000")))
        for item in filter(None, os.environ.get("FAKE_QUOTES", "SPY:500,TSLA:364.2").split(",")):
            sym, px = item.split(":")
            fb.set_quote(sym.strip(), float(px))
        return fb
    if kind == "ib":
        from ibkr_exec.broker.ib import IBBroker  # imported here only

        return IBBroker(cfg.ib.host, cfg.ib.port, cfg.ib.client_id, clock)
    raise SystemExit(f"unknown BROKER={kind!r}")


def build() -> tuple[FastAPI, Service]:
    setup_logging()
    cfg = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    clock = SystemClock()
    repo = Repository(make_engine(_require_env("DATABASE_URL")))
    alerter = SlackAlerter(cfg.alerts.slack_webhook_url, repo, clock, mode=cfg.mode)
    broker = _make_broker(cfg, clock)
    svc = Service(cfg, broker, repo, clock, alerter)
    secrets = {"bot": _require_env("HMAC_BOT_SECRET"), "admin": _require_env("HMAC_ADMIN_SECRET")}
    if secrets["bot"] == secrets["admin"]:
        log.error("hmac_secrets_identical")
        sys.exit(2)
    app = create_app(svc, secrets)
    scheduler = build_scheduler(svc)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if os.environ.get("BROKER", "ib").lower() == "fake":
            await repo.create_all()
        try:
            await svc.start()
        except ModeMismatch as exc:
            log.error("mode_mismatch", error=str(exc))
            await alerter.alert("critical", "Startup refused", str(exc))
            raise SystemExit(3) from exc
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await svc.stop()
            await repo.dispose()

    app.router.lifespan_context = lifespan
    return app, svc


def main() -> None:
    app, _ = build()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
