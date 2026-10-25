"""Tiny in-memory sliding-window rate limiter keyed by client IP. Good enough for a demo."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, seconds_until_reset)."""
        now = time.monotonic()
        window = 60.0
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= self.per_minute:
                return False, int(window - (now - q[0])) + 1
            q.append(now)
            return True, 0
