"""HMAC-SHA256 request authentication with replay protection.

Headers: ``X-Key-Id`` (``bot`` | ``admin``), ``X-Timestamp`` (unix seconds),
``X-Signature`` (hex). The signed string is
``METHOD \\n PATH \\n TIMESTAMP \\n sha256hex(body)``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_SKEW_S = 30


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash(body)}".encode()


def sign(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), canonical(method, path, timestamp, body), "sha256").hexdigest()


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class Verifier:
    secrets: dict[str, str]  # key_id -> secret
    max_skew_s: float = MAX_SKEW_S
    _seen: OrderedDict[tuple[str, str], float] = field(default_factory=OrderedDict)

    def verify(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        key_id: str | None,
        timestamp: str | None,
        signature: str | None,
        now: float | None = None,
        allowed_keys: tuple[str, ...] = ("bot", "admin"),
    ) -> str:
        now = time.time() if now is None else now
        if not key_id or not timestamp or not signature:
            raise AuthError(401, "missing auth headers")
        secret = self.secrets.get(key_id)
        if key_id not in allowed_keys or not secret:
            raise AuthError(403, "key not allowed for this endpoint")
        try:
            ts = float(timestamp)
        except ValueError as exc:
            raise AuthError(401, "bad timestamp") from exc
        if abs(now - ts) > self.max_skew_s:
            raise AuthError(401, "timestamp outside allowed window")
        expected = sign(secret, method, path, timestamp, body)
        if not hmac.compare_digest(expected, signature.lower()):
            raise AuthError(401, "bad signature")
        self._purge(now)
        key = (key_id, signature.lower())
        if key in self._seen:
            raise AuthError(401, "replayed request")
        self._seen[key] = now
        return key_id

    def _purge(self, now: float) -> None:
        horizon = now - 2 * self.max_skew_s
        while self._seen:
            k, t = next(iter(self._seen.items()))
            if t < horizon:
                self._seen.popitem(last=False)
            else:
                break
