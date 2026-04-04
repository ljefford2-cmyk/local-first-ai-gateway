"""In-memory sliding window RPM rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindowRateLimiter:
    """Per-route sliding window rate limiter (requests per minute)."""

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check(self, route_id: str, rpm_limit: int) -> bool:
        """Return True if the request is allowed, False if rate limited."""
        now = time.monotonic()
        cutoff = now - 60.0

        timestamps = self._windows[route_id]
        # Prune expired entries
        self._windows[route_id] = [t for t in timestamps if t > cutoff]

        if len(self._windows[route_id]) >= rpm_limit:
            return False

        self._windows[route_id].append(now)
        return True

    def current_count(self, route_id: str) -> int:
        """Return current request count in the window."""
        now = time.monotonic()
        cutoff = now - 60.0
        self._windows[route_id] = [t for t in self._windows[route_id] if t > cutoff]
        return len(self._windows[route_id])
