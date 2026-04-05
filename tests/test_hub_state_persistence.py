"""Persistence tests for the hub state manager (HubStateManager).

Covers: SQLite write-through, restart survival, and graceful fallback
to in-memory when the DB is unavailable.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from hub_state import HubState, HubStateManager


def _create_db(db_path: str) -> None:
    """Create the ``state`` table needed by HubStateManager."""
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


# -- Restart survival --------------------------------------------------------


def test_suspended_state_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = HubStateManager(db_path=db_path)
    assert mgr1.state == HubState.ACTIVE
    mgr1.suspend()
    assert mgr1.state == HubState.SUSPENDED

    # Restart — new instance, same DB
    mgr2 = HubStateManager(db_path=db_path)
    assert mgr2.state == HubState.SUSPENDED


def test_active_state_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = HubStateManager(db_path=db_path)
    mgr1.suspend()
    mgr1.resume()
    assert mgr1.state == HubState.ACTIVE

    mgr2 = HubStateManager(db_path=db_path)
    assert mgr2.state == HubState.ACTIVE


def test_awaiting_authority_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = HubStateManager(db_path=db_path, authority_timeout_seconds=0)
    # startup_self_check with no heartbeat → AWAITING_AUTHORITY
    mgr1.startup_self_check()
    assert mgr1.state == HubState.AWAITING_AUTHORITY

    mgr2 = HubStateManager(db_path=db_path)
    assert mgr2.state == HubState.AWAITING_AUTHORITY


def test_confirm_authority_persists(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = HubStateManager(db_path=db_path, authority_timeout_seconds=0)
    mgr1.startup_self_check()
    assert mgr1.state == HubState.AWAITING_AUTHORITY
    mgr1.confirm_authority()
    assert mgr1.state == HubState.ACTIVE

    mgr2 = HubStateManager(db_path=db_path)
    assert mgr2.state == HubState.ACTIVE


def test_heartbeat_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = HubStateManager(db_path=db_path)
    mgr1.record_heartbeat()
    elapsed1 = mgr1.seconds_since_last_heartbeat()
    assert elapsed1 is not None
    assert elapsed1 < 2.0

    # Restart — heartbeat should be approximately preserved
    mgr2 = HubStateManager(db_path=db_path)
    elapsed2 = mgr2.seconds_since_last_heartbeat()
    assert elapsed2 is not None
    # Allow some tolerance for the round-trip
    assert elapsed2 < 5.0


def test_processing_gate_survives_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = HubStateManager(db_path=db_path)
    mgr1.suspend()
    assert mgr1.is_processing_allowed() is False

    mgr2 = HubStateManager(db_path=db_path)
    assert mgr2.is_processing_allowed() is False


# -- Fallback to in-memory ---------------------------------------------------


def test_fallback_when_db_path_invalid():
    mgr = HubStateManager(db_path="/nonexistent/dir/test.db")
    assert mgr._db is None

    # Should still work in-memory
    mgr.suspend()
    assert mgr.state == HubState.SUSPENDED
    mgr.resume()
    assert mgr.state == HubState.ACTIVE


def test_fallback_when_table_missing(tmp_path):
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    mgr = HubStateManager(db_path=db_path)
    assert mgr._db is None

    mgr.suspend()
    assert mgr.state == HubState.SUSPENDED


def test_no_db_path_is_pure_in_memory():
    """HubStateManager with no db_path behaves identically to the
    original in-memory-only implementation."""
    mgr = HubStateManager(db_path=None)
    assert mgr._db is None

    assert mgr.state == HubState.ACTIVE
    mgr.suspend()
    assert mgr.state == HubState.SUSPENDED
    assert mgr.is_processing_allowed() is False
    mgr.resume()
    assert mgr.state == HubState.ACTIVE
    assert mgr.is_processing_allowed() is True
    mgr.record_heartbeat()
    assert mgr.seconds_since_last_heartbeat() < 2.0
