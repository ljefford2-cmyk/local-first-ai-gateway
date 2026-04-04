"""Phase 7C unit tests — Cloud Connectivity Monitor + Circuit Breaker.

Covers: CircuitState, RouteHealth, ConnectivityMonitor with hysteresis,
event emission, background probe loop, and query API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from connectivity_monitor import (
    CircuitState,
    ConnectivityMonitor,
    FAILURES_TO_OPEN,
    RouteHealth,
    SUCCESSES_TO_CLOSED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_routes(*route_ids: str) -> list[dict]:
    """Build minimal route configs for testing."""
    return [
        {
            "route_id": rid,
            "endpoint_url": f"http://fake-{rid}.test/v1",
            "health": {"timeout_ms": 500},
        }
        for rid in route_ids
    ]


def _make_monitor(
    route_ids: list[str] | None = None,
    queued_count: int = 0,
) -> tuple[ConnectivityMonitor, list[dict]]:
    """Return (monitor, emitted_events_list)."""
    if route_ids is None:
        route_ids = ["route-a"]
    events: list[dict] = []
    monitor = ConnectivityMonitor(
        routes=_make_routes(*route_ids),
        emit_event=events.append,
        get_queued_count=lambda: queued_count,
    )
    return monitor, events


def _force_result(monitor: ConnectivityMonitor, route_id: str, success: bool) -> None:
    """Directly record a probe result without HTTP."""
    now = datetime.now(timezone.utc)
    rh = monitor._health[route_id]
    rh.last_probe_time = now
    monitor._record_result(route_id, success, now)


# ---------------------------------------------------------------------------
# 1. RouteHealth initializes with CLOSED state and zero counters
# ---------------------------------------------------------------------------

def test_route_health_defaults():
    rh = RouteHealth(route_id="r1")
    assert rh.state is CircuitState.CLOSED
    assert rh.consecutive_failures == 0
    assert rh.consecutive_successes == 0
    assert rh.last_probe_time is None
    assert rh.last_success_time is None
    assert rh.last_failure_time is None


# ---------------------------------------------------------------------------
# 2. Single probe failure does not change state from CLOSED
# ---------------------------------------------------------------------------

def test_single_failure_stays_closed():
    mon, events = _make_monitor()
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.CLOSED
    assert len(events) == 0


# ---------------------------------------------------------------------------
# 3. Two consecutive probe failures transition to OPEN
# ---------------------------------------------------------------------------

def test_two_failures_open():
    mon, events = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.OPEN
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 4. Single probe success after OPEN does not change state
# ---------------------------------------------------------------------------

def test_single_success_after_open_goes_half_open():
    mon, _ = _make_monitor()
    # Move to OPEN
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.OPEN
    # One success -> HALF_OPEN, not CLOSED
    _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].state is CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# 5. Two consecutive successes after OPEN transition to HALF_OPEN
# ---------------------------------------------------------------------------

def test_two_successes_after_open_still_half_open():
    mon, _ = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=True)
    _force_result(mon, "route-a", success=True)
    # 2 successes is still < SUCCESSES_TO_CLOSED (3)
    assert mon._health["route-a"].state is CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# 6. Three consecutive successes transition from OPEN to CLOSED
# ---------------------------------------------------------------------------

def test_three_successes_close_circuit():
    mon, _ = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.OPEN
    for _ in range(SUCCESSES_TO_CLOSED):
        _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 7. State transition CLOSED -> OPEN emits event with state "down"
# ---------------------------------------------------------------------------

def test_closed_to_open_emits_down():
    mon, events = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert len(events) == 1
    assert events[0]["payload"]["state"] == "down"


# ---------------------------------------------------------------------------
# 8. State transition OPEN -> CLOSED emits event with state "up"
# ---------------------------------------------------------------------------

def test_open_to_closed_emits_up():
    mon, events = _make_monitor()
    # CLOSED -> OPEN
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    events.clear()
    # OPEN -> HALF_OPEN -> CLOSED
    for _ in range(SUCCESSES_TO_CLOSED):
        _force_result(mon, "route-a", success=True)
    # Should have HALF_OPEN (degraded) + CLOSED (up)
    up_events = [e for e in events if e["payload"]["state"] == "up"]
    assert len(up_events) == 1


# ---------------------------------------------------------------------------
# 9. HALF_OPEN emits connectivity event with state "degraded"
# ---------------------------------------------------------------------------

def test_half_open_emits_degraded():
    mon, events = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    events.clear()
    _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].state is CircuitState.HALF_OPEN
    assert len(events) == 1
    assert events[0]["payload"]["state"] == "degraded"


# ---------------------------------------------------------------------------
# 10. Probe success resets consecutive_failures counter
# ---------------------------------------------------------------------------

def test_success_resets_failures():
    mon, _ = _make_monitor()
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].consecutive_failures == 1
    _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].consecutive_failures == 0


# ---------------------------------------------------------------------------
# 11. Probe failure resets consecutive_successes counter
# ---------------------------------------------------------------------------

def test_failure_resets_successes():
    mon, _ = _make_monitor()
    _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].consecutive_successes == 1
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].consecutive_successes == 0


# ---------------------------------------------------------------------------
# 12. is_route_available returns True for CLOSED
# ---------------------------------------------------------------------------

def test_is_available_closed():
    mon, _ = _make_monitor()
    assert mon.is_route_available("route-a") is True


# ---------------------------------------------------------------------------
# 13. is_route_available returns True for HALF_OPEN
# ---------------------------------------------------------------------------

def test_is_available_half_open():
    mon, _ = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].state is CircuitState.HALF_OPEN
    assert mon.is_route_available("route-a") is True


# ---------------------------------------------------------------------------
# 14. is_route_available returns False for OPEN
# ---------------------------------------------------------------------------

def test_is_available_open():
    mon, _ = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.OPEN
    assert mon.is_route_available("route-a") is False


# ---------------------------------------------------------------------------
# 15. get_available_routes returns only CLOSED and HALF_OPEN routes
# ---------------------------------------------------------------------------

def test_get_available_routes():
    mon, _ = _make_monitor(route_ids=["r1", "r2", "r3"])
    # r1: CLOSED (default)
    # r2: OPEN
    _force_result(mon, "r2", success=False)
    _force_result(mon, "r2", success=False)
    # r3: HALF_OPEN
    _force_result(mon, "r3", success=False)
    _force_result(mon, "r3", success=False)
    _force_result(mon, "r3", success=True)

    available = mon.get_available_routes()
    assert "r1" in available
    assert "r2" not in available
    assert "r3" in available


# ---------------------------------------------------------------------------
# 16. last_successful_cloud_probe_timestamp returns most recent success
# ---------------------------------------------------------------------------

def test_last_success_timestamp_most_recent():
    mon, _ = _make_monitor(route_ids=["r1", "r2"])
    _force_result(mon, "r1", success=True)
    _force_result(mon, "r2", success=True)
    ts = mon.last_successful_cloud_probe_timestamp
    assert ts is not None
    # The most recent should be r2 (called last)
    assert ts == mon._health["r2"].last_success_time


# ---------------------------------------------------------------------------
# 17. last_successful_cloud_probe_timestamp returns None when no probes succeeded
# ---------------------------------------------------------------------------

def test_last_success_timestamp_none():
    mon, _ = _make_monitor()
    assert mon.last_successful_cloud_probe_timestamp is None


# ---------------------------------------------------------------------------
# 18. Connectivity event includes affected_routes list
# ---------------------------------------------------------------------------

def test_event_includes_affected_routes():
    mon, events = _make_monitor()
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert events[0]["payload"]["affected_routes"] == ["route-a"]


# ---------------------------------------------------------------------------
# 19. Connectivity event includes queued_jobs count
# ---------------------------------------------------------------------------

def test_event_includes_queued_jobs():
    events: list[dict] = []
    mon = ConnectivityMonitor(
        routes=_make_routes("route-a"),
        emit_event=events.append,
        get_queued_count=lambda: 42,
    )
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert events[0]["payload"]["queued_jobs"] == 42


# ---------------------------------------------------------------------------
# 20. Multiple routes tracked independently
# ---------------------------------------------------------------------------

def test_routes_independent():
    mon, _ = _make_monitor(route_ids=["r1", "r2"])
    # Fail r1 into OPEN
    _force_result(mon, "r1", success=False)
    _force_result(mon, "r1", success=False)
    # r2 stays CLOSED
    _force_result(mon, "r2", success=True)

    assert mon._health["r1"].state is CircuitState.OPEN
    assert mon._health["r2"].state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 21. Route that was never probed starts as CLOSED (optimistic)
# ---------------------------------------------------------------------------

def test_never_probed_starts_closed():
    mon, _ = _make_monitor(route_ids=["r1"])
    rh = mon.get_route_health("r1")
    assert rh is not None
    assert rh.state is CircuitState.CLOSED
    assert rh.last_probe_time is None


# ---------------------------------------------------------------------------
# 22. Probe timeout is treated as failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_timeout_is_failure():
    mon, _ = _make_monitor()
    # Patch httpx to raise a timeout
    with patch("connectivity_monitor.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.head = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client_cls.return_value = mock_client

        result = await mon.probe_route("route-a")

    assert result is False
    assert mon._health["route-a"].consecutive_failures == 1


# ---------------------------------------------------------------------------
# 23. Background monitor start/stop lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monitor_start_stop():
    mon, _ = _make_monitor()
    # Patch probe_route to be a no-op
    mon.probe_route = AsyncMock()

    await mon.start_monitoring(interval_seconds=0.05)
    assert mon._monitor_task is not None
    assert not mon._monitor_task.done()

    # Let it run a tick
    await asyncio.sleep(0.1)

    await mon.stop_monitoring()
    assert mon._monitor_task is None


# ---------------------------------------------------------------------------
# 24. Hysteresis prevents flapping: alternating success/failure stays HALF_OPEN
# ---------------------------------------------------------------------------

def test_hysteresis_prevents_flapping():
    mon, _ = _make_monitor()
    # Get to OPEN first
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.OPEN

    # Alternating results — never accumulates enough consecutive successes
    for _ in range(5):
        _force_result(mon, "route-a", success=True)
        # After one success from OPEN -> HALF_OPEN
        state = mon._health["route-a"].state
        assert state in (CircuitState.HALF_OPEN, CircuitState.OPEN)
        _force_result(mon, "route-a", success=False)

    # Should end at OPEN (2 consecutive failures accumulate because
    # each failure resets successes, and the alternation means we
    # get fail after a success, resetting consecutive_successes to 0,
    # then consecutive_failures hits 2 on the second pass through)
    # Actually let's verify: last iteration does success then failure.
    # After success: consecutive_successes=1, consecutive_failures=0 -> HALF_OPEN
    # After failure: consecutive_successes=0, consecutive_failures=1 -> stays HALF_OPEN
    # Next iteration: success -> cs=1, cf=0 -> HALF_OPEN; failure -> cs=0, cf=1 -> HALF_OPEN
    # But wait - on the failure, consecutive_failures only goes to 1, which is <2.
    # So alternating success/failure keeps it in HALF_OPEN after initial transition.
    # That's exactly the hysteresis behavior we want.
    rh = mon._health["route-a"]
    assert rh.state in (CircuitState.HALF_OPEN, CircuitState.OPEN)
    # It should never have reached CLOSED during alternation
    assert rh.consecutive_successes < SUCCESSES_TO_CLOSED


# ---------------------------------------------------------------------------
# 25. State transitions are logged
# ---------------------------------------------------------------------------

def test_state_transitions_logged(caplog):
    mon, _ = _make_monitor()
    with caplog.at_level(logging.INFO, logger="connectivity_monitor"):
        _force_result(mon, "route-a", success=False)
        _force_result(mon, "route-a", success=False)
    assert any("circuit" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 26. Probe with unknown route returns False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_unknown_route():
    mon, _ = _make_monitor()
    result = await mon.probe_route("nonexistent")
    assert result is False


# ---------------------------------------------------------------------------
# 27. get_route_health returns None for unknown route
# ---------------------------------------------------------------------------

def test_get_route_health_unknown():
    mon, _ = _make_monitor()
    assert mon.get_route_health("nonexistent") is None


# ---------------------------------------------------------------------------
# 28. is_route_available returns False for unknown route
# ---------------------------------------------------------------------------

def test_is_available_unknown_route():
    mon, _ = _make_monitor()
    assert mon.is_route_available("nonexistent") is False


# ---------------------------------------------------------------------------
# 29. Full recovery cycle: CLOSED -> OPEN -> HALF_OPEN -> CLOSED
# ---------------------------------------------------------------------------

def test_full_recovery_cycle():
    mon, events = _make_monitor()
    # CLOSED -> OPEN
    _force_result(mon, "route-a", success=False)
    _force_result(mon, "route-a", success=False)
    assert mon._health["route-a"].state is CircuitState.OPEN

    # OPEN -> HALF_OPEN -> CLOSED
    for _ in range(SUCCESSES_TO_CLOSED):
        _force_result(mon, "route-a", success=True)
    assert mon._health["route-a"].state is CircuitState.CLOSED

    # Events: down, degraded, up
    states = [e["payload"]["state"] for e in events]
    assert states == ["down", "degraded", "up"]


# ---------------------------------------------------------------------------
# 30. Successful probe via HTTP returns True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_success_via_http():
    mon, _ = _make_monitor()
    with patch("connectivity_monitor.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.head = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await mon.probe_route("route-a")

    assert result is True
    assert mon._health["route-a"].consecutive_successes == 1


# ---------------------------------------------------------------------------
# 31. HTTP 5xx is treated as failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_5xx_is_failure():
    mon, _ = _make_monitor()
    with patch("connectivity_monitor.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client.head = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await mon.probe_route("route-a")

    assert result is False
    assert mon._health["route-a"].consecutive_failures == 1
