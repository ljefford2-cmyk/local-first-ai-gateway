"""Phase 7B unit tests — Idempotency Store + JobManager integration.

Covers: IdempotencyStore CRUD, TTL purge, thread safety, and
JobManager.submit_job idempotency enforcement.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from idempotency_store import DEFAULT_TTL_SECONDS, IdempotencyRecord, IdempotencyStore
from job_manager import JobManager
from test_helpers import MockAuditClient

# UUIDv7 pattern
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ==========================================================================
# IdempotencyStore unit tests
# ==========================================================================


# 1. New key is accepted and stored
def test_new_key_accepted():
    store = IdempotencyStore()
    is_new, existing = store.check_and_store("key-1", "job-1")
    assert is_new is True
    assert existing is None


# 2. Duplicate key is rejected
def test_duplicate_key_rejected():
    store = IdempotencyStore()
    store.check_and_store("key-1", "job-1")
    is_new, existing = store.check_and_store("key-1", "job-2")
    assert is_new is False
    assert existing == "job-1"


# 3. Different keys are independent
def test_different_keys_independent():
    store = IdempotencyStore()
    ok1, _ = store.check_and_store("key-a", "job-a")
    ok2, _ = store.check_and_store("key-b", "job-b")
    assert ok1 is True
    assert ok2 is True
    assert store.get("key-a").job_id == "job-a"
    assert store.get("key-b").job_id == "job-b"


# 4. get() returns record for stored key
def test_get_returns_record():
    store = IdempotencyStore()
    store.check_and_store("key-1", "job-1")
    rec = store.get("key-1")
    assert rec is not None
    assert rec.job_id == "job-1"
    assert rec.status == "accepted"
    assert isinstance(rec.created_at, datetime)


# 5. get() returns None for unknown key
def test_get_returns_none_for_unknown():
    store = IdempotencyStore()
    assert store.get("nonexistent") is None


# 6. update_status() changes stored status
def test_update_status():
    store = IdempotencyStore()
    store.check_and_store("key-1", "job-1")
    store.update_status("key-1", "delivered")
    assert store.get("key-1").status == "delivered"


# 7. update_status() on unknown key is a no-op
def test_update_status_unknown_key_no_op():
    store = IdempotencyStore()
    # Should not raise
    store.update_status("nonexistent", "delivered")
    assert store.get("nonexistent") is None


# 8. purge_expired() removes entries older than TTL
def test_purge_expired_removes_old():
    store = IdempotencyStore()
    store.check_and_store("key-old", "job-old")
    # Backdate the record
    store._records["key-old"].created_at = datetime.now(timezone.utc) - timedelta(days=8)
    purged = store.purge_expired()
    assert purged == 1
    assert store.get("key-old") is None


# 9. purge_expired() keeps entries within TTL
def test_purge_expired_keeps_fresh():
    store = IdempotencyStore()
    store.check_and_store("key-fresh", "job-fresh")
    purged = store.purge_expired()
    assert purged == 0
    assert store.get("key-fresh") is not None


# 10. purge_expired() with custom TTL
def test_purge_expired_custom_ttl():
    store = IdempotencyStore()
    store.check_and_store("key-1", "job-1")
    # Backdate by 2 seconds
    store._records["key-1"].created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    # TTL of 1 second should purge it
    purged = store.purge_expired(ttl_seconds=1)
    assert purged == 1
    assert store.get("key-1") is None


# 11. Empty store purge is a no-op
def test_purge_empty_store():
    store = IdempotencyStore()
    purged = store.purge_expired()
    assert purged == 0


# 12. Thread safety: concurrent check_and_store calls
def test_thread_safety_concurrent():
    store = IdempotencyStore()
    results: dict[int, tuple[bool, str | None]] = {}
    num_threads = 50

    def worker(idx: int):
        # All threads use the same key — only one should succeed
        is_new, existing = store.check_and_store("shared-key", f"job-{idx}")
        results[idx] = (is_new, existing)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread should have stored the key
    new_count = sum(1 for is_new, _ in results.values() if is_new)
    assert new_count == 1

    # All others should get the same existing job_id
    winner_job_id = store.get("shared-key").job_id
    for idx, (is_new, existing) in results.items():
        if not is_new:
            assert existing == winner_job_id


# 13. IdempotencyRecord stores job_id, created_at, status
def test_idempotency_record_fields():
    now = datetime.now(timezone.utc)
    rec = IdempotencyRecord(job_id="j1", created_at=now, status="accepted")
    assert rec.job_id == "j1"
    assert rec.created_at == now
    assert rec.status == "accepted"


# ==========================================================================
# JobManager integration tests
# ==========================================================================


def _make_manager() -> tuple[JobManager, MockAuditClient]:
    audit = MockAuditClient()
    mgr = JobManager(audit_client=audit)
    return mgr, audit


# 14. submit_job with idempotency_key creates job normally
def test_submit_job_with_key():
    mgr, audit = _make_manager()
    job = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="idem-1"))
    assert job.job_id is not None
    assert job.status == "submitted"
    assert job.idempotency_key == "idem-1"


# 15. submit_job with same key returns same job on second call
def test_submit_job_duplicate_key_returns_same():
    mgr, audit = _make_manager()
    job1 = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="idem-dup"))
    job2 = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="idem-dup"))
    assert job1.job_id == job2.job_id
    assert job1 is job2


# 16. submit_job with same key does NOT emit duplicate job.submitted event
def test_submit_job_duplicate_no_extra_event():
    mgr, audit = _make_manager()
    _run(mgr.submit_job("hello", "text", "watch", idempotency_key="idem-evt"))
    event_count_after_first = len(audit.get_events_by_type("job.submitted"))
    _run(mgr.submit_job("hello", "text", "watch", idempotency_key="idem-evt"))
    event_count_after_second = len(audit.get_events_by_type("job.submitted"))
    assert event_count_after_first == 1
    assert event_count_after_second == 1  # no new event


# 17. submit_job without idempotency_key auto-generates one
def test_submit_job_auto_generates_key():
    mgr, audit = _make_manager()
    job = _run(mgr.submit_job("hello", "text", "watch"))
    assert job.idempotency_key is not None
    assert len(job.idempotency_key) > 0


# 18. submit_job stores idempotency_key on Job object
def test_submit_job_stores_key_on_job():
    mgr, audit = _make_manager()
    job = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="idem-stored"))
    assert job.idempotency_key == "idem-stored"
    # Also accessible via get_job
    fetched = mgr.get_job(job.job_id)
    assert fetched.idempotency_key == "idem-stored"


# 19. Auto-generated idempotency key is UUIDv7 format
def test_auto_generated_key_is_uuidv7():
    mgr, audit = _make_manager()
    job = _run(mgr.submit_job("hello", "text", "watch"))
    assert UUID_V7_RE.match(job.idempotency_key), (
        f"Auto-generated key is not UUIDv7: {job.idempotency_key}"
    )


# 20. Store handles large number of keys without error
def test_store_handles_many_keys():
    store = IdempotencyStore()
    for i in range(1500):
        is_new, _ = store.check_and_store(f"key-{i}", f"job-{i}")
        assert is_new is True
    assert len(store) == 1500
    # Spot-check a few
    assert store.get("key-0").job_id == "job-0"
    assert store.get("key-999").job_id == "job-999"
    assert store.get("key-1499").job_id == "job-1499"


# ==========================================================================
# Bonus coverage
# ==========================================================================


# 21. Default TTL constant is 7 days
def test_default_ttl_is_7_days():
    assert DEFAULT_TTL_SECONDS == 604800
    assert DEFAULT_TTL_SECONDS == 7 * 24 * 60 * 60


# 22. Two different idempotency keys produce different jobs
def test_different_keys_different_jobs():
    mgr, audit = _make_manager()
    job1 = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="k1"))
    job2 = _run(mgr.submit_job("hello", "text", "watch", idempotency_key="k2"))
    assert job1.job_id != job2.job_id


# 23. Idempotency store __len__ reflects stored count
def test_store_len():
    store = IdempotencyStore()
    assert len(store) == 0
    store.check_and_store("a", "ja")
    assert len(store) == 1
    store.check_and_store("b", "jb")
    assert len(store) == 2
    # Duplicate doesn't increase count
    store.check_and_store("a", "ja2")
    assert len(store) == 2
