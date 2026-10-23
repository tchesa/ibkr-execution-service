"""Authentication and rate limiting as middleware, so they run before body validation.

Every path except ``/health`` needs a valid HMAC signature. Admin paths need the admin
key. ``/signals`` is rate limited per key.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ibkr_exec.api.auth import AuthError, Verifier
from ibkr_exec.api.ratelimit import RateLimiter

PUBLIC_PATHS = {"/health"}
ADMIN_PATHS = {"/halt", "/resume", "/flatten"}
RATE_LIMITED_PATHS = {"/signals"}


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, verifier: Verifier, limiter: RateLimiter, clock) -> None:
        super().__init__(app)
        self._verifier = verifier
        self._limiter = limiter
        self._clock = clock

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS:
            return await call_next(request)
        body = await request.body()  # cached by Starlette; the route reads the same bytes
        allowed = ("admin",) if path in ADMIN_PATHS else ("bot", "admin")
        try:
            key = self._verifier.verify(
                method=request.method,
                path=path,
                body=body,
                key_id=request.headers.get("x-key-id"),
                timestamp=request.headers.get("x-timestamp"),
                signature=request.headers.get("x-signature"),
                now=self._clock.now().timestamp(),
                allowed_keys=allowed,
            )
        except AuthError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)
        if path in RATE_LIMITED_PATHS and request.method == "POST" and not self._limiter.allow(key):
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        request.state.key_id = key
        return await call_next(request)
