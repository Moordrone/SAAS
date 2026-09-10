"""Fixed-window rate limiting.

In-memory by default, which is correct for one process and wrong for several —
each worker would allow the full quota. The store is behind an interface so
Redis can replace it without touching the middleware; until then, set the limit
low enough that per-process leakage still bounds the total.

Account lockout already stops brute force against a single account. This stops
the other shape: one attempt each against a hundred thousand accounts.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class FixedWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        """Return (allowed, seconds until reset)."""
        now = time.time()
        cutoff = now - window
        with self._lock:
            hits = [t for t in self._hits[key] if t > cutoff]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False, int(hits[0] + window - now) + 1
            hits.append(now)
            self._hits[key] = hits
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def prune(self, older_than: int = 3600) -> None:
        """Keys are created per client; without this the dict grows forever."""
        cutoff = time.time() - older_than
        with self._lock:
            for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                del self._hits[key]


limiter = FixedWindowLimiter()

#: Endpoints where an attacker gains something by trying repeatedly.
AUTH_PATHS = (
    "/v1/auth/login",
    "/v1/auth/signup",
    "/v1/auth/password-reset/request",
    "/v1/auth/password-reset/confirm",
    "/v1/auth/refresh",
    "/v1/waitlist",
)
