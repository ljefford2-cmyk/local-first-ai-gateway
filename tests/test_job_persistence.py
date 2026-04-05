"""Persistence tests for the job store.

Covers: SQLite write-through, restart survival, terminal job filtering,
status update persistence, stale recovery with persistence, and graceful
fallback to in-memory when the DB is unavailable.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from job_manager import JobManager
from models import Job, JobStatus
from test_helpers import MockAuditClient


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _create_db(db_path: str) -> None:
    """Create the tables needed by JobManager and IdempotencyStore."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            data JSON,
            status TEXT,
            created_at TEXT,
            idempotency_key TEXT UNIQUE
        )"""
    )
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


def test_jobs_survive_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("hello", "text", "watch"))
    original_id = job.job_id

    # Simulate restart — new manager, same DB
    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    recovered = mgr2.get_job(original_id)
    assert recovered is not None
    assert recovered.job_id == original_id
    assert recovered.status == "submitted"
    assert recovered.raw_input == "hello"


def test_terminal_jobs_not_loaded_on_startup(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("hello", "text", "watch"))
    job_id = job.job_id

    # Transition to delivered (terminal) and persist
    job.status = JobStatus.delivered.value
    job.delivered_at = datetime.now(timezone.utc).isoformat()
    mgr1._persist_job(job)

    # Restart — terminal job should NOT be loaded
    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    assert mgr2.get_job(job_id) is None


def test_failed_jobs_not_loaded_on_startup(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("hello", "text", "watch"))
    job_id = job.job_id

    job.status = JobStatus.failed.value
    job.error = "test_error"
    mgr1._persist_job(job)

    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    assert mgr2.get_job(job_id) is None


def test_revoked_jobs_not_loaded_on_startup(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("hello", "text", "watch"))
    job_id = job.job_id

    job.status = JobStatus.revoked.value
    mgr1._persist_job(job)

    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    assert mgr2.get_job(job_id) is None


# -- Status update persistence -----------------------------------------------


def test_status_updates_persisted(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("hello", "text", "watch"))
    original_id = job.job_id

    # Update status and persist
    job.status = JobStatus.classified.value
    job.classified_at = datetime.now(timezone.utc).isoformat()
    mgr1._persist_job(job)

    # Restart and verify
    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    recovered = mgr2.get_job(original_id)
    assert recovered is not None
    assert recovered.status == JobStatus.classified.value
    assert recovered.classified_at is not None


def test_multiple_jobs_survive_restart(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    ids = []
    for i in range(5):
        job = _run(mgr1.submit_job(f"input-{i}", "text", "watch"))
        ids.append(job.job_id)

    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    for jid in ids:
        assert mgr2.get_job(jid) is not None


# -- Stale recovery with persistence -----------------------------------------


def test_stale_recovery_finds_persisted_jobs(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    # Create a job in submitted state
    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("recover me", "text", "watch"))
    job_id = job.job_id

    # Simulate restart — new manager loads from DB
    audit2 = MockAuditClient()
    mgr2 = JobManager(audit_client=audit2, db_path=db_path)
    assert mgr2.get_job(job_id) is not None

    # Run stale recovery — should find the submitted job
    from stale_recovery import StaleJobRecovery

    recovery = StaleJobRecovery(audit2)
    report = _run(recovery.run_recovery(mgr2._jobs, mgr2))

    assert report.jobs_scanned >= 1
    recovered_actions = [
        a for a in report.actions if a.action_taken not in ("no_action",)
    ]
    recovered_ids = [a.job_id for a in recovered_actions]
    assert job_id in recovered_ids


def test_stale_recovery_ignores_terminal_jobs(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr1 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job = _run(mgr1.submit_job("done job", "text", "watch"))

    # Mark as delivered
    job.status = JobStatus.delivered.value
    job.delivered_at = datetime.now(timezone.utc).isoformat()
    mgr1._persist_job(job)

    # Restart — terminal jobs not loaded, recovery has nothing to do
    audit2 = MockAuditClient()
    mgr2 = JobManager(audit_client=audit2, db_path=db_path)

    from stale_recovery import StaleJobRecovery

    recovery = StaleJobRecovery(audit2)
    report = _run(recovery.run_recovery(mgr2._jobs, mgr2))

    assert report.jobs_scanned == 0


# -- Fallback to in-memory ---------------------------------------------------


def test_fallback_when_db_unavailable():
    mgr = JobManager(
        audit_client=MockAuditClient(), db_path="/nonexistent/dir/test.db"
    )
    assert mgr._job_db is None

    # Should still work in-memory
    job = _run(mgr.submit_job("hello", "text", "watch"))
    assert job is not None
    assert mgr.get_job(job.job_id) is not None


def test_fallback_when_table_missing(tmp_path):
    # Create a DB without the jobs table
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.close()

    mgr = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    assert mgr._job_db is None

    job = _run(mgr.submit_job("hello", "text", "watch"))
    assert job is not None


# -- Existing lifecycle preserved with persistence ----------------------------


def test_idempotency_works_with_persistence(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    job1 = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="k1"))
    job2 = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="k1"))
    assert job1.job_id == job2.job_id


def test_get_job_returns_none_for_unknown(tmp_path):
    db_path = str(tmp_path / "test.db")
    _create_db(db_path)

    mgr = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    assert mgr.get_job("nonexistent") is None
