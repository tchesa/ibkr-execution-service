"""Token bucket per key: ``rate`` requests per ``per`` seconds."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    rate: int = 10
    per: float = 60.0
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (tokens, ts)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, ts = self._buckets.get(key, (float(self.rate), now))
        tokens = min(float(self.rate), tokens + (now - ts) * self.rate / self.per)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True
