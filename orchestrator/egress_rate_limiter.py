"""Per-worker egress rate limiter for Phase 6C.

Extends the Spec 4 rate limiter concept to be per-worker-scoped.
Each worker (identified by blueprint_id) gets its own independent
sliding window counter. Rate limits are inherited from the Spec 4
egress policy for the governing capability.

Worker A's request count does NOT affect Worker B.
"""

from __future__ import annotations

import time
from collections import defaultdict


class EgressRateLimiter:
    """Per-worker sliding window rate limiter (requests per minute).

    Each blueprint_id gets an independent counter. The rate limit
    (rpm) is provided per-call from the EgressPolicy binding.
    """

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check(self, blueprint_id: str, rpm_limit: int) -> bool:
        """Return True if the request is allowed, False if rate limited.

        Args:
            blueprint_id: The worker's blueprint ID (scoping key).
            rpm_limit: Maximum requests per minute from the EgressPolicy.
        """
        now = time.monotonic()
        cutoff = now - 60.0

        timestamps = self._windows[blueprint_id]
        # Prune expired entries
        self._windows[blueprint_id] = [t for t in timestamps if t > cutoff]

        if len(self._windows[blueprint_id]) >= rpm_limit:
            return False

        self._windows[blueprint_id].append(now)
        return True

    def current_count(self, blueprint_id: str) -> int:
        """Return current request count in the 60-second window."""
        now = time.monotonic()
        cutoff = now - 60.0
        self._windows[blueprint_id] = [
            t for t in self._windows[blueprint_id] if t > cutoff
        ]
        return len(self._windows[blueprint_id])

    def reset(self, blueprint_id: str) -> None:
        """Reset the counter for a specific worker."""
        self._windows[blueprint_id] = []
