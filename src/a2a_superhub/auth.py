from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    reason: str = "ok"


class BearerAuth:
    """Optional bearer-token auth for local and private deployments."""

    def __init__(self, token: str | None = None):
        self.token = token

    def check(self, header_value: str | None) -> AuthResult:
        if not self.token:
            return AuthResult(True)
        expected = f"Bearer {self.token}"
        if header_value == expected:
            return AuthResult(True)
        return AuthResult(False, "missing or invalid bearer token")


class FixedWindowLimiter:
    """Tiny in-memory limiter for the public MVP server."""

    def __init__(self, limit: int = 120, window_seconds: int = 60):
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        bucket = self._events[key]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True
