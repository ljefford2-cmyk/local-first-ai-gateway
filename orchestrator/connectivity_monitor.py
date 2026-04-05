"""Cloud connectivity monitor with per-route circuit breakers.

Implements Spec 7 Section 2 failure detection and Section 2.1 state
transition hysteresis for egress routes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

import httpx

from events import event_system_connectivity_hub_cloud

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"        # Route healthy, traffic flows
    OPEN = "open"            # Route failed, traffic blocked
    HALF_OPEN = "half_open"  # Testing recovery


# Hysteresis thresholds (Spec 7 Section 2.1)
FAILURES_TO_OPEN = 2
SUCCESSES_TO_CLOSED = 3


@dataclass
class RouteHealth:
    """Per-route health state with circuit breaker."""

    route_id: str
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_probe_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None
    last_failure_time: Optional[datetime] = None


class ConnectivityMonitor:
    """Monitors cloud route health with circuit breakers and hysteresis.

    Implements Spec 7 Section 2 failure detection and Section 2.1
    state transition hysteresis:
    - 2 consecutive failures -> OPEN (route down)
    - 3 consecutive successes -> CLOSED (route up)
    - Between: HALF_OPEN (degraded)
    """

    _KEY_PREFIX = "circuit:"

    def __init__(
        self,
        routes: list[dict[str, Any]],
        emit_event: Callable[[dict[str, Any]], Any],
        get_queued_count: Callable[[], int] | None = None,
        db_path: str | None = None,
    ) -> None:
        """Initialize the monitor.

        Args:
            routes: List of route dicts, each with at least ``route_id``,
                ``endpoint_url``, and optionally ``health`` with
                ``timeout_ms`` and a ``health_endpoint`` override.
            emit_event: Callable that accepts an event dict and emits it
                (e.g. ``audit_client.emit_durable``).
            get_queued_count: Optional callable returning the current number
                of queued cloud-bound jobs.
            db_path: Path to SQLite database for persistence. Resolved from
                the persistence module if not provided.
        """
        self._routes_config: dict[str, dict[str, Any]] = {}
        self._health: dict[str, RouteHealth] = {}
        self._emit_event = emit_event
        self._get_queued_count = get_queued_count or (lambda: 0)
        self._monitor_task: asyncio.Task | None = None

        # Resolve db_path: explicit arg → persistence module → None (in-memory)
        if db_path is None:
            try:
                from persistence import get_db_path
                db_path = get_db_path()
            except ImportError:
                pass

        self._db: sqlite3.Connection | None = None
        if db_path is not None:
            try:
                self._db = sqlite3.connect(db_path, check_same_thread=False)
                self._db.execute("SELECT 1 FROM state LIMIT 1")
                logger.info("Circuit breaker store using SQLite at %s", db_path)
            except Exception:
                logger.warning(
                    "SQLite unavailable — falling back to in-memory circuit breaker store",
                    exc_info=True,
                )
                self._db = None

        for route in routes:
            rid = route["route_id"]
            self._routes_config[rid] = route
            self._health[rid] = RouteHealth(route_id=rid)

        self._load_from_db()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_from_db(self) -> None:
        """Populate in-memory RouteHealth cache from the database."""
        if self._db is None:
            return
        try:
            cursor = self._db.execute(
                "SELECT key, value FROM state WHERE key LIKE ?",
                (self._KEY_PREFIX + "%",),
            )
            for row in cursor:
                route_id = row[0][len(self._KEY_PREFIX):]
                rh = self._health.get(route_id)
                if rh is None:
                    continue
                data = json.loads(row[1])
                rh.state = CircuitState(data["state"])
                rh.consecutive_failures = data["consecutive_failures"]
                rh.consecutive_successes = data["consecutive_successes"]
                if data.get("last_success_time"):
                    rh.last_success_time = datetime.fromisoformat(data["last_success_time"])
                if data.get("last_failure_time"):
                    rh.last_failure_time = datetime.fromisoformat(data["last_failure_time"])
                if data.get("last_probe_time"):
                    rh.last_probe_time = datetime.fromisoformat(data["last_probe_time"])
            logger.info("Loaded circuit breaker state from database")
        except Exception:
            logger.warning("Failed to load circuit breaker state from DB", exc_info=True)

    def _db_write(self, route_id: str, rh: RouteHealth) -> None:
        """Write-through a single route's health state to SQLite."""
        if self._db is None:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            value = json.dumps({
                "state": rh.state.value,
                "consecutive_failures": rh.consecutive_failures,
                "consecutive_successes": rh.consecutive_successes,
                "last_success_time": rh.last_success_time.isoformat() if rh.last_success_time else None,
                "last_failure_time": rh.last_failure_time.isoformat() if rh.last_failure_time else None,
                "last_probe_time": rh.last_probe_time.isoformat() if rh.last_probe_time else None,
            })
            self._db.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (self._KEY_PREFIX + route_id, value, now),
            )
            self._db.commit()
        except Exception:
            logger.warning("DB write failed for circuit %s", route_id, exc_info=True)

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_route_health(self, route_id: str) -> RouteHealth | None:
        """Return the RouteHealth for a given route, or None."""
        return self._health.get(route_id)

    def is_route_available(self, route_id: str) -> bool:
        """True if circuit is CLOSED or HALF_OPEN (allow traffic for testing)."""
        rh = self._health.get(route_id)
        if rh is None:
            return False
        return rh.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def get_available_routes(self) -> list[str]:
        """All route IDs with CLOSED or HALF_OPEN circuits."""
        return [
            rid for rid, rh in self._health.items()
            if rh.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)
        ]

    @property
    def last_successful_cloud_probe_timestamp(self) -> datetime | None:
        """Most recent last_success_time across all routes."""
        latest: datetime | None = None
        for rh in self._health.values():
            if rh.last_success_time is not None:
                if latest is None or rh.last_success_time > latest:
                    latest = rh.last_success_time
        return latest

    # ------------------------------------------------------------------
    # Probe execution
    # ------------------------------------------------------------------

    async def probe_route(self, route_id: str) -> bool:
        """Probe a single route and update its health state.

        Returns True on success, False on failure.
        """
        config = self._routes_config.get(route_id)
        if config is None:
            return False

        rh = self._health[route_id]
        now = datetime.now(timezone.utc)
        rh.last_probe_time = now

        health_cfg = config.get("health", {})
        timeout_ms = health_cfg.get("timeout_ms", 10000)
        timeout_s = timeout_ms / 1000.0

        # Use health_endpoint if configured, else main endpoint_url
        url = health_cfg.get("health_endpoint") or config.get("endpoint_url", "")

        success = False
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.head(url)
                if resp.status_code < 500:
                    success = True
        except Exception:
            success = False

        self._record_result(route_id, success, now)
        return success

    def _record_result(
        self, route_id: str, success: bool, now: datetime,
    ) -> None:
        """Update counters and trigger state transitions."""
        rh = self._health[route_id]
        old_state = rh.state

        if success:
            rh.consecutive_successes += 1
            rh.consecutive_failures = 0
            rh.last_success_time = now
        else:
            rh.consecutive_failures += 1
            rh.consecutive_successes = 0
            rh.last_failure_time = now

        # Hysteresis state machine (Spec 7 Section 2.1)
        new_state = old_state

        if rh.consecutive_failures >= FAILURES_TO_OPEN:
            new_state = CircuitState.OPEN
        elif rh.consecutive_successes >= SUCCESSES_TO_CLOSED:
            new_state = CircuitState.CLOSED
        elif old_state == CircuitState.OPEN and rh.consecutive_successes >= 1:
            # At least one success after OPEN but not enough for CLOSED
            new_state = CircuitState.HALF_OPEN
        elif old_state == CircuitState.CLOSED and rh.consecutive_failures == 1:
            # Single failure from CLOSED: stay CLOSED (hysteresis)
            new_state = CircuitState.CLOSED

        rh.state = new_state
        self._db_write(route_id, rh)

        if new_state != old_state:
            self._on_state_change(route_id, old_state, new_state)

    def _on_state_change(
        self,
        route_id: str,
        old_state: CircuitState,
        new_state: CircuitState,
    ) -> None:
        """Emit a connectivity event and log the transition."""
        state_map = {
            CircuitState.OPEN: "down",
            CircuitState.CLOSED: "up",
            CircuitState.HALF_OPEN: "degraded",
        }
        event_state = state_map[new_state]

        evt = event_system_connectivity_hub_cloud(
            state=event_state,
            affected_routes=[route_id],
            queued_jobs=self._get_queued_count(),
        )
        self._emit_event(evt)

        logger.info(
            "Route %s circuit: %s -> %s",
            route_id, old_state.value, new_state.value,
        )

    # ------------------------------------------------------------------
    # Background monitoring loop
    # ------------------------------------------------------------------

    async def start_monitoring(self, interval_seconds: float = 30) -> None:
        """Start the background probe loop."""
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(
            self._probe_loop(interval_seconds),
        )
        logger.info(
            "Connectivity monitor started (interval=%ss, routes=%d)",
            interval_seconds, len(self._routes_config),
        )

    async def stop_monitoring(self) -> None:
        """Cancel the background probe loop."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
            logger.info("Connectivity monitor stopped")

    async def _probe_loop(self, interval: float) -> None:
        """Continuously probe all routes at the given interval."""
        while True:
            for route_id in list(self._routes_config):
                try:
                    await self.probe_route(route_id)
                except Exception:
                    logger.exception("Probe error for route %s", route_id)
            await asyncio.sleep(interval)
