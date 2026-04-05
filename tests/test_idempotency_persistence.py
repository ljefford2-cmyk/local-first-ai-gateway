"""Persistence tests for the idempotency store.

Covers: SQLite write-through, restart survival, TTL cleanup against the
database, and graceful fallback to in-memory when the DB is unavailable.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from idempotency_store import IdempotencyStore


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _create_db(db_path: str) -> None:
    """Create the ``state`` table needed by IdempotencyStore."""
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


# -- Restart survival ------------------------------------------------------


def test_records_survive_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    store1 = IdempotencyStore(db_path=db_path)
    is_new, _ = store1.check_and_store("key-1", "job-1")
    assert is_new is True

    # Simulate restart — new store instance, same DB
    store2 = IdempotencyStore(db_path=db_path)
    is_new, existing = store2.check_and_store("key-1", "job-2")
    assert is_new is False
    assert existing == "job-1"


def test_record_status_preserved_across_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    store1 = IdempotencyStore(db_path=db_path)
    store1.check_and_store("key-1", "job-1")
    store1.update_status("key-1", "delivered")

    store2 = IdempotencyStore(db_path=db_path)
    rec = store2.get("key-1")
    assert rec is not None
    assert rec.job_id == "job-1"
    assert rec.status == "delivered"
    assert isinstance(rec.created_at, datetime)


def test_multiple_records_survive_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    store1 = IdempotencyStore(db_path=db_path)
    for i in range(10):
        store1.check_and_store(f"key-{i}", f"job-{i}")

    store2 = IdempotencyStore(db_path=db_path)
    assert len(store2) == 10
    for i in range(10):
        rec = store2.get(f"key-{i}")
        assert rec is not None
        assert rec.job_id == f"job-{i}"


# -- TTL cleanup against DB -----------------------------------------------


def test_ttl_cleanup_removes_from_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    store1 = IdempotencyStore(db_path=db_path)
    store1.check_and_store("key-old", "job-old")
    store1.check_and_store("key-fresh", "job-fresh")

    # Backdate one record in memory (DB write happened with original time)
    store1._records["key-old"].created_at = datetime.now(timezone.utc) - timedelta(days=8)

    purged = store1.purge_expired()
    assert purged == 1

    # Verify purged from DB — new store should not see it
    store2 = IdempotencyStore(db_path=db_path)
    assert store2.get("key-old") is None
    assert store2.get("key-fresh") is not None
    assert len(store2) == 1


# -- Fallback to in-memory -------------------------------------------------


def test_fallback_when_db_path_invalid():
    store = IdempotencyStore(db_path="/nonexistent/dir/test.db")
    assert store._db is None

    # Store should still work in-memory
    is_new, _ = store.check_and_store("key-1", "job-1")
    assert is_new is True
    rec = store.get("key-1")
    assert rec is not None
    assert rec.job_id == "job-1"


def test_fallback_when_table_missing(tmp_path):
    # Create a DB file but without the state table
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    store = IdempotencyStore(db_path=db_path)
    assert store._db is None  # should have fallen back

    is_new, _ = store.check_and_store("key-1", "job-1")
    assert is_new is True


# -- init_db integration ---------------------------------------------------


def test_init_db_creates_tables(tmp_path):
    import persistence

    db_path = str(tmp_path / "test.db")
    _run(persistence.init_db(db_path))
    try:
        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "jobs" in table_names
        assert "state" in table_names
        conn.close()
    finally:
        _run(persistence.close_db())


def test_store_works_with_init_db(tmp_path):
    import persistence

    db_path = str(tmp_path / "test.db")
    _run(persistence.init_db(db_path))
    try:
        store = IdempotencyStore(db_path=db_path)
        is_new, _ = store.check_and_store("key-1", "job-1")
        assert is_new is True
        assert store.get("key-1").job_id == "job-1"
    finally:
        _run(persistence.close_db())


# -- Default (no DB) matches original behaviour ----------------------------


def test_no_db_path_is_pure_in_memory():
    """IdempotencyStore() with no db_path and no init_db call behaves
    identically to the original in-memory-only implementation."""
    store = IdempotencyStore(db_path=None)
    assert store._db is None

    is_new, _ = store.check_and_store("k", "j")
    assert is_new is True
    assert store.get("k").job_id == "j"
    store.update_status("k", "done")
    assert store.get("k").status == "done"
    assert len(store) == 1
    store._records["k"].created_at = datetime.now(timezone.utc) - timedelta(days=8)
    assert store.purge_expired() == 1
    assert len(store) == 0
