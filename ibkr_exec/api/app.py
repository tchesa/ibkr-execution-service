"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from ibkr_exec.api.auth import Verifier
from ibkr_exec.api.middleware import AuthMiddleware
from ibkr_exec.api.ratelimit import RateLimiter
from ibkr_exec.api.routes import router
from ibkr_exec.service import Service


def create_app(svc: Service, secrets: dict[str, str], signals_per_minute: int = 10) -> FastAPI:
    app = FastAPI(title="IBKR execution service", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = svc
    app.state.verifier = Verifier(secrets)
    app.state.limiter = RateLimiter(rate=signals_per_minute, per=60)
    app.add_middleware(
        AuthMiddleware, verifier=app.state.verifier, limiter=app.state.limiter, clock=svc.clock
    )
    app.include_router(router)
    return app
