"""In-memory rate limiting, scope 12.

"Only enough to stop obvious abuse." State lives in the process, so it resets on
restart and would not survive a second worker -- both fine for one container
serving 5-8 friends, and both wrong the moment this is scaled out. Documented
rather than engineered around.

Used for login now; the per-user submit cap in slice 4 uses the same limiter.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def check(self, key: str) -> bool:
        """True if the caller is within budget, without consuming it."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            self._prune(bucket, now)
            return len(bucket) < self.limit

    def hit(self, key: str) -> bool:
        """Record an attempt. Returns False once the budget is spent."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            self._prune(bucket, now)
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            self._prune(bucket, now)
            if not bucket:
                return 0
            return max(1, int(self.window - (now - bucket[0])) + 1)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


# Ten failed logins per five minutes, keyed on username+client address. Cleared
# on a successful login so a player who mistypes twice then succeeds is not
# still carrying a penalty.
login_limiter = RateLimiter(limit=10, window_seconds=300)

#: Scope 12: at most one submission per user per 10 seconds. This exists to stop
#: a runaway client loop writing thousands of PNGs, not to police pace.
submit_limiter = RateLimiter(limit=1, window_seconds=10)


def client_key(request, *parts: str) -> str:
    client = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        client = forwarded.split(",")[0].strip()
    return "|".join([client, *parts])
