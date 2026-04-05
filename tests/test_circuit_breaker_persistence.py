"""Persistence tests for the circuit breaker (ConnectivityMonitor).

Covers: SQLite write-through, restart survival, and graceful fallback
to in-memory when the DB is unavailable.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from connectivity_monitor import CircuitState, ConnectivityMonitor, RouteHealth


def _create_db(db_path: str) -> None:
    """Create the ``state`` table needed by ConnectivityMonitor."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value JSON,
            updated_at TEXT
        )"""
    )
    conn.commit()
    conn.close()


def _make_routes(*names: str) -> list[dict]:
    return [
        {"route_id": n, "endpoint_url": f"http://{n}.example.com"}
        for n in names
    ]


def _noop_emit(evt):
    pass


# -- Restart survival --------------------------------------------------------


def test_circuit_state_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    routes = _make_routes("route-a")
    mon1 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)

    # Simulate 2 failures → OPEN
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    mon1._record_result("route-a", False, now)
    mon1._record_result("route-a", False, now)
    assert mon1.get_route_health("route-a").state == CircuitState.OPEN

    # Restart — new instance, same DB
    mon2 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)
    rh = mon2.get_route_health("route-a")
    assert rh.state == CircuitState.OPEN
    assert rh.consecutive_failures == 2
    assert rh.consecutive_successes == 0


def test_half_open_state_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    routes = _make_routes("route-a")
    mon1 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # 2 failures → OPEN
    mon1._record_result("route-a", False, now)
    mon1._record_result("route-a", False, now)
    # 1 success → HALF_OPEN
    mon1._record_result("route-a", True, now)
    assert mon1.get_route_health("route-a").state == CircuitState.HALF_OPEN

    mon2 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)
    assert mon2.get_route_health("route-a").state == CircuitState.HALF_OPEN


def test_closed_state_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    routes = _make_routes("route-a")
    mon1 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # 2 failures → OPEN, then 3 successes → CLOSED
    mon1._record_result("route-a", False, now)
    mon1._record_result("route-a", False, now)
    mon1._record_result("route-a", True, now)
    mon1._record_result("route-a", True, now)
    mon1._record_result("route-a", True, now)
    assert mon1.get_route_health("route-a").state == CircuitState.CLOSED

    mon2 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)
    rh = mon2.get_route_health("route-a")
    assert rh.state == CircuitState.CLOSED
    assert rh.consecutive_successes == 3


def test_multiple_routes_survive_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    routes = _make_routes("route-a", "route-b")
    mon1 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    # route-a → OPEN
    mon1._record_result("route-a", False, now)
    mon1._record_result("route-a", False, now)
    # route-b stays CLOSED (1 success)
    mon1._record_result("route-b", True, now)

    mon2 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)
    assert mon2.get_route_health("route-a").state == CircuitState.OPEN
    assert mon2.get_route_health("route-b").state == CircuitState.CLOSED


def test_timestamps_survive_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    routes = _make_routes("route-a")
    mon1 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    mon1._record_result("route-a", True, now)

    mon2 = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)
    rh = mon2.get_route_health("route-a")
    assert rh.last_success_time is not None


# -- Fallback to in-memory ---------------------------------------------------


def test_fallback_when_db_path_invalid():
    routes = _make_routes("route-a")
    mon = ConnectivityMonitor(routes, _noop_emit, db_path="/nonexistent/dir/test.db")
    assert mon._db is None

    # Should still work in-memory
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    mon._record_result("route-a", False, now)
    mon._record_result("route-a", False, now)
    assert mon.get_route_health("route-a").state == CircuitState.OPEN


def test_fallback_when_table_missing(tmp_path):
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    routes = _make_routes("route-a")
    mon = ConnectivityMonitor(routes, _noop_emit, db_path=db_path)
    assert mon._db is None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    mon._record_result("route-a", False, now)
    assert mon.get_route_health("route-a").consecutive_failures == 1


def test_no_db_path_is_pure_in_memory():
    """ConnectivityMonitor with no db_path behaves identically to the
    original in-memory-only implementation."""
    routes = _make_routes("route-a")
    mon = ConnectivityMonitor(routes, _noop_emit, db_path=None)
    assert mon._db is None

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    mon._record_result("route-a", False, now)
    mon._record_result("route-a", False, now)
    assert mon.get_route_health("route-a").state == CircuitState.OPEN
    assert mon.is_route_available("route-a") is False
