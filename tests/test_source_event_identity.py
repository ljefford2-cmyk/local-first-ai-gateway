"""§6B-1 / §6B-2 source-event identity — submit-boundary behavior tests.

This is the Insert D "free" bucket: every test runs in-process with a
``MockAuditClient`` and (at most) a temp SQLite file. No job is driven to
completion and the local model is never called, so these run first and cheaply.

Because this file lives under ``tests/`` it is subject to the session
stack-health gate in ``tests/conftest.py``; run the free bucket with
``pytest tests/test_source_event_identity.py --noconftest`` (no live stack
needed). See ``docs/plans/6b-source-event-identity.md`` and the cost-aware
buckets in the spec inserts.

Covers §11.8 (normalization replay/dedup + changed-intent conflict) and §11.9
(interim 409 contract). The matching local-model bucket (equivalent replay
*after* proposal_ready / delivery via the real pipeline) lives in
``tests/test_source_event_identity_e2e.py`` and is run after this passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from idempotency_store import IdempotencyStore, SourceEventRecord
from job_manager import (
    JobManager,
    SourceEventConsistencyPending,
    SourceEventIntentConflict,
)
from models import ClientSource, JobStatus, JobSubmitRequest
from source_event import compute_intent_equivalence_hash, source_event_key
from test_helpers import MockAuditClient


# ---------- scaffolding ----------

def _make_manager(audit: MockAuditClient, db_path: str | None = None) -> JobManager:
    return JobManager(audit_client=audit, db_path=db_path)


def _create_db(db_path: str) -> None:
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


def _submit_kwargs(**overrides) -> dict:
    base = {
        "raw_input": "remind me to call John",
        "input_modality": "text",
        "device": "phone",
        "idempotency_key": "idem-1",
        "client_source": "phone_app",
        "client_source_event_id": "evt-1",
        "client_timestamp": "2026-06-01T12:00:00Z",
    }
    base.update(overrides)
    return base


# ---------- §11.8: equivalent replay dedups ----------

@pytest.mark.asyncio
async def test_equivalent_replay_dedups_across_whitespace_and_different_idem_key():
    """Same source event, normalized-equivalent intent, *different* idem key →
    dedup to the original job (the exact gap the truth report proved: today a
    different idem key makes a duplicate)."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job1 = await jm.submit_job(
        **_submit_kwargs(raw_input="  remind   me to call John  ", idempotency_key="k1")
    )
    job2 = await jm.submit_job(
        **_submit_kwargs(raw_input="remind me to call John", idempotency_key="k2-diff")
    )

    assert job2.job_id == job1.job_id
    assert len(jm._jobs) == 1
    assert len(audit.get_events_by_type("job.submitted")) == 1
    # Exactly one classification was enqueued — the replay did not re-enqueue.
    assert jm._queue.qsize() == 1


@pytest.mark.asyncio
async def test_equivalent_replay_survives_envelope_drift():
    """device and client_timestamp are not part of the intent hash, so drifting
    them on replay still dedups."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job1 = await jm.submit_job(**_submit_kwargs(device="phone", client_timestamp="2026-06-01T12:00:00Z"))
    job2 = await jm.submit_job(
        **_submit_kwargs(
            idempotency_key="k2", device="watch", client_timestamp="2026-06-01T23:59:00Z"
        )
    )

    assert job2.job_id == job1.job_id
    assert len(jm._jobs) == 1


@pytest.mark.asyncio
async def test_two_distinct_source_events_create_two_jobs():
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job1 = await jm.submit_job(**_submit_kwargs(client_source_event_id="evt-1"))
    job2 = await jm.submit_job(
        **_submit_kwargs(client_source_event_id="evt-2", idempotency_key="k2")
    )

    assert job1.job_id != job2.job_id
    assert len(jm._jobs) == 2


# ---------- §11.8 / §11.9: changed-intent conflict ----------

@pytest.mark.asyncio
async def test_changed_intent_raises_conflict_with_locked_body():
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job1 = await jm.submit_job(**_submit_kwargs(raw_input="remind me to call John"))

    with pytest.raises(SourceEventIntentConflict) as excinfo:
        await jm.submit_job(
            **_submit_kwargs(raw_input="remind me to text John", idempotency_key="k2-diff")
        )

    body = excinfo.value.body
    assert body["source_event_replay"] is True
    assert body["source_event_conflict"] is True
    assert body["conflict_type"] == "intent_mismatch"
    assert body["original_job_id"] == job1.job_id
    assert body["action_required"] == "reconfirmation_required"
    assert body["adr_compliance"] == "partial_until_reconfirmation_path_exists"
    assert "reconfirmation" in body["message"].lower()


@pytest.mark.asyncio
async def test_changed_intent_blocks_duplicate_and_preserves_mapping_and_audits():
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job1 = await jm.submit_job(**_submit_kwargs(raw_input="remind me to call John"))
    original_hash = job1.intent_equivalence_hash

    with pytest.raises(SourceEventIntentConflict):
        await jm.submit_job(
            **_submit_kwargs(raw_input="remind me to text John", idempotency_key="k2-diff")
        )

    # No duplicate executing job, no extra submit, no extra classification.
    assert len(jm._jobs) == 1
    assert len(audit.get_events_by_type("job.submitted")) == 1
    assert jm._queue.qsize() == 1

    # Original mapping preserved (not overwritten with the changed intent).
    rec = jm._idempotency_store.get_source_event("phone_app", "evt-1")
    assert rec is not None
    assert rec.job_id == job1.job_id
    assert rec.intent_equivalence_hash == original_hash

    # Conflict audit event emitted with both hashes.
    conflicts = audit.get_events_by_type("source_event.conflict")
    assert len(conflicts) == 1
    payload = conflicts[0]["payload"]
    assert payload["original_job_id"] == job1.job_id
    assert payload["conflict_type"] == "intent_mismatch"
    assert payload["original_intent_equivalence_hash"] == original_hash
    assert payload["new_intent_equivalence_hash"] != original_hash
    assert payload["adr_compliance"] == "partial_until_reconfirmation_path_exists"


# ---------- §6B-1: lineage persistence ----------

@pytest.mark.asyncio
async def test_lineage_persisted_on_job_and_submitted_event():
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = await jm.submit_job(**_submit_kwargs())

    assert job.client_source == "phone_app"
    assert job.client_source_event_id == "evt-1"
    assert job.client_timestamp == "2026-06-01T12:00:00Z"
    expected_hash = compute_intent_equivalence_hash("remind me to call John", "text")
    assert job.intent_equivalence_hash == expected_hash

    submitted = audit.get_events_by_type("job.submitted")[0]
    payload = submitted["payload"]
    assert payload["client_source"] == "phone_app"
    assert payload["client_source_event_id"] == "evt-1"
    assert payload["client_timestamp"] == "2026-06-01T12:00:00Z"
    assert payload["intent_equivalence_hash"] == expected_hash

    # Durable mapping recorded.
    rec = jm._idempotency_store.get_source_event("phone_app", "evt-1")
    assert rec is not None and rec.job_id == job.job_id


@pytest.mark.asyncio
async def test_backward_compat_no_client_fields_keeps_server_envelope_and_no_mapping():
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = await jm.submit_job(
        raw_input="remind me to call John",
        input_modality="text",
        device="phone",
        idempotency_key="legacy-1",
    )

    assert job.client_source is None
    assert job.client_source_event_id is None
    assert job.intent_equivalence_hash is None

    submitted = audit.get_events_by_type("job.submitted")[0]
    assert submitted["payload"]["client_source"] is None
    assert submitted["payload"]["intent_equivalence_hash"] is None
    # Envelope stays server-generated.
    assert submitted["source"] == "orchestrator"
    assert submitted["source_event_id"]
    assert submitted["timestamp"]

    # No source-event mapping was created.
    assert jm._idempotency_store._source_event_records == {}


# ---------- §6B-2 durability: equivalent replay after terminal + restart ----------

@pytest.mark.asyncio
async def test_equivalent_replay_after_terminal_and_restart_returns_original(tmp_path):
    """The core durability fix (truth report point #1): once a job is terminal
    and the orchestrator restarts, the submit idempotency_key is purged — but the
    durable source-event identity must still dedup the same source event to the
    original job instead of creating a duplicate."""
    db_path = str(tmp_path / "drnt.db")
    _create_db(db_path)

    audit1 = MockAuditClient()
    jm1 = _make_manager(audit1, db_path=db_path)
    job1 = await jm1.submit_job(**_submit_kwargs(idempotency_key="k1"))

    # Drive to a terminal state and persist (no model needed).
    job1.status = JobStatus.delivered.value
    jm1._persist_job(job1)

    # Simulate restart: a fresh manager on the same DB.
    audit2 = MockAuditClient()
    jm2 = _make_manager(audit2, db_path=db_path)

    # Terminal job is not reloaded into memory; the submit idem key was purged…
    assert job1.job_id not in jm2._jobs
    # …but the durable source-event mapping survived.
    assert jm2._idempotency_store.get_source_event("phone_app", "evt-1") is not None

    replay = await jm2.submit_job(**_submit_kwargs(idempotency_key="k2-diff"))

    assert replay.job_id == job1.job_id
    assert replay.status == JobStatus.delivered.value
    # No duplicate job, no new submit event, nothing enqueued.
    assert len(audit2.get_events_by_type("job.submitted")) == 0
    assert job1.job_id not in jm2._jobs


# ---------- §11.9: route surface returns 409, never 202 ----------

@pytest.mark.asyncio
async def test_route_returns_409_for_changed_intent_not_202():
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    saved = main_module.job_manager
    main_module.job_manager = jm
    try:
        first = await main_module.submit_job(
            JobSubmitRequest(
                raw_input="remind me to call John",
                input_modality="text",
                device="phone",
                idempotency_key="k1",
                client_source=ClientSource.phone_app,
                client_source_event_id="evt-route",
                client_timestamp="2026-06-01T12:00:00Z",
            )
        )
        # First submit is accepted (the route decorator stamps 202); the handler
        # returns a JobSubmitResponse, not a conflict Response.
        assert getattr(first, "status", None) == "submitted"

        conflict = await main_module.submit_job(
            JobSubmitRequest(
                raw_input="remind me to text John",
                input_modality="text",
                device="phone",
                idempotency_key="k2-diff",
                client_source=ClientSource.phone_app,
                client_source_event_id="evt-route",
                client_timestamp="2026-06-01T12:00:00Z",
            )
        )
        assert conflict.status_code == 409
        body = json.loads(conflict.body)
        assert body["source_event_conflict"] is True
        assert body["conflict_type"] == "intent_mismatch"
        assert body["original_job_id"] == first.job_id
        assert body["adr_compliance"] == "partial_until_reconfirmation_path_exists"
    finally:
        main_module.job_manager = saved


# ---------- §6B-2 concurrency hardening ----------


class _SlowAudit(MockAuditClient):
    """Durable emit blocks, widening the reserve→materialize window so a
    concurrent identical submit would race without the per-source-event lock."""

    def __init__(self, delay: float = 0.2):
        super().__init__()
        self._delay = delay

    async def emit_durable(self, event: dict) -> bool:
        await asyncio.sleep(self._delay)
        return await super().emit_durable(event)


class _FailingAudit(MockAuditClient):
    """Durable emit always fails (audit unavailable)."""

    async def emit_durable(self, event: dict) -> bool:
        raise TimeoutError("audit unavailable")


@pytest.mark.asyncio
async def test_concurrent_identical_submits_create_one_job():
    """Two simultaneous submits of the same source event (different idem keys,
    normalized-equivalent intent) must produce exactly ONE job; the loser returns
    the original. The slow audit keeps the winner in flight across the window
    that, unguarded, produced a duplicate."""
    audit = _SlowAudit(delay=0.2)
    jm = _make_manager(audit)

    job_a, job_b = await asyncio.gather(
        jm.submit_job(
            **_submit_kwargs(raw_input="  remind   me to call John  ", idempotency_key="kA")
        ),
        jm.submit_job(
            **_submit_kwargs(raw_input="remind me to call John", idempotency_key="kB")
        ),
    )

    assert job_a.job_id == job_b.job_id
    assert len(jm._jobs) == 1
    assert len(audit.get_events_by_type("job.submitted")) == 1
    assert jm._queue.qsize() == 1


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_reservation_so_retry_creates_job():
    """A submit that fails before the job is durable (audit down) must roll back
    its reservation, so a later retry creates the job cleanly instead of being
    permanently stuck on a poisoned mapping."""
    jm = _make_manager(_FailingAudit())

    with pytest.raises(TimeoutError):
        await jm.submit_job(**_submit_kwargs(idempotency_key="k1"))

    assert jm._idempotency_store.get_source_event("phone_app", "evt-1") is None
    assert len(jm._jobs) == 0

    # Retry with a working audit creates the job cleanly.
    jm._audit = MockAuditClient()
    job = await jm.submit_job(**_submit_kwargs(idempotency_key="k2"))
    assert job.client_source == "phone_app"
    assert len(jm._jobs) == 1
    rec = jm._idempotency_store.get_source_event("phone_app", "evt-1")
    assert rec is not None and rec.job_id == job.job_id


@pytest.mark.asyncio
async def test_unresolvable_equivalent_mapping_fails_closed_no_duplicate():
    """If a mapping exists but its job cannot be loaded, submit must FAIL CLOSED
    (retryable) and create no new job — the prohibited fall-through."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    # Inject a mapping pointing at a job that exists nowhere.
    h = compute_intent_equivalence_hash("remind me to call John", "text")
    jm._idempotency_store._source_event_records[
        source_event_key("phone_app", "evt-ghost")
    ] = SourceEventRecord(
        job_id="ghost-job",
        intent_equivalence_hash=h,
        client_source="phone_app",
        client_source_event_id="evt-ghost",
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(SourceEventConsistencyPending) as excinfo:
        await jm.submit_job(
            **_submit_kwargs(client_source_event_id="evt-ghost", idempotency_key="kX")
        )

    body = excinfo.value.body
    assert body["consistency_pending"] is True
    assert body["original_job_id"] == "ghost-job"
    assert body["retryable"] is True
    # The invariant: no duplicate job was created.
    assert len(jm._jobs) == 0
    assert audit.get_events_by_type("job.submitted") == []


@pytest.mark.asyncio
async def test_route_returns_503_for_consistency_pending():
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    h = compute_intent_equivalence_hash("remind me to call John", "text")
    jm._idempotency_store._source_event_records[
        source_event_key("phone_app", "evt-ghost2")
    ] = SourceEventRecord(
        job_id="ghost2",
        intent_equivalence_hash=h,
        client_source="phone_app",
        client_source_event_id="evt-ghost2",
        created_at=datetime.now(timezone.utc),
    )
    saved = main_module.job_manager
    main_module.job_manager = jm
    try:
        resp = await main_module.submit_job(
            JobSubmitRequest(
                raw_input="remind me to call John",
                input_modality="text",
                device="phone",
                idempotency_key="kX",
                client_source=ClientSource.phone_app,
                client_source_event_id="evt-ghost2",
                client_timestamp="2026-06-01T12:00:00Z",
            )
        )
        assert resp.status_code == 503
        body = json.loads(resp.body)
        assert body["consistency_pending"] is True
        assert body["original_job_id"] == "ghost2"
    finally:
        main_module.job_manager = saved


# ---------- §6B Layer 1: DB-arbitrated first-writer (store-level) ----------


def test_db_reserve_source_event_is_first_writer_wins(tmp_path):
    db_path = str(tmp_path / "drnt.db")
    _create_db(db_path)
    store = IdempotencyStore(db_path=db_path)
    h1 = "hash-1"

    s1, r1 = store.check_and_store_source_event("phone_app", "evtA", "jobA", h1)
    assert s1 == "new" and r1.job_id == "jobA"

    # Same key, same hash, different candidate → equivalent; original preserved.
    s2, r2 = store.check_and_store_source_event("phone_app", "evtA", "jobB", h1)
    assert s2 == "equivalent" and r2.job_id == "jobA"

    # Same key, different hash → conflict; original preserved.
    s3, r3 = store.check_and_store_source_event("phone_app", "evtA", "jobC", "hash-2")
    assert s3 == "conflict" and r3.job_id == "jobA"

    # A fresh store on the same DB sees exactly the first writer, durably.
    store2 = IdempotencyStore(db_path=db_path)
    rec = store2.get_source_event("phone_app", "evtA")
    assert rec is not None and rec.job_id == "jobA" and rec.intent_equivalence_hash == h1


def test_db_first_writer_guard_across_two_store_instances(tmp_path):
    """Cross-process guard: two SEPARATE IdempotencyStore instances on the SAME
    SQLite DB (no shared memory — the in-memory lock cannot help) both attempt to
    reserve the SAME source-event key. The DB
    ``INSERT ... ON CONFLICT(key) DO NOTHING`` guard must let exactly ONE win
    ``new``; the loser must READ the winner's record and must NOT clobber it.

    This is the direct two-store proof of Required Fix #3 — the property that
    ``INSERT OR REPLACE`` (last-writer-wins) silently violated. Sequential by
    construction (matching the prompt's store-A-then-store-B shape); the guard is
    atomic so the same outcome holds when the two attempts truly interleave.
    """
    db_path = str(tmp_path / "drnt.db")
    _create_db(db_path)

    store_a = IdempotencyStore(db_path=db_path)
    store_b = IdempotencyStore(db_path=db_path)

    # Both attempt the same key + same intent hash, different candidate job_ids
    # (as two processes racing the same source event would).
    sa, ra = store_a.check_and_store_source_event("phone_app", "evtX", "jobA", "h")
    sb, rb = store_b.check_and_store_source_event("phone_app", "evtX", "jobB", "h")

    # Exactly one winner; store_a inserted first, so it is the durable winner.
    assert sorted([sa, sb]) == ["equivalent", "new"]
    assert (sa, ra.job_id) == ("new", "jobA")
    # The loser READ the winner's record — it did not keep its own candidate and
    # did not clobber the DB row.
    assert (sb, rb.job_id) == ("equivalent", "jobA")

    # No clobber, durably: a third fresh store sees only the first writer.
    store_c = IdempotencyStore(db_path=db_path)
    rec = store_c.get_source_event("phone_app", "evtX")
    assert rec is not None and rec.job_id == "jobA" and rec.intent_equivalence_hash == "h"

    # A changed-intent attempt by the loser store is a conflict, never a clobber.
    sb2, rb2 = store_b.check_and_store_source_event("phone_app", "evtX", "jobB2", "h2")
    assert (sb2, rb2.job_id) == ("conflict", "jobA")
    assert store_c.get_source_event("phone_app", "evtX").job_id == "jobA"


def test_release_source_event_compare_and_delete(tmp_path):
    db_path = str(tmp_path / "drnt.db")
    _create_db(db_path)
    store = IdempotencyStore(db_path=db_path)
    store.check_and_store_source_event("phone_app", "evtR", "jobR", "h")

    # Wrong job_id must not delete another writer's reservation.
    store.release_source_event("phone_app", "evtR", "not-jobR")
    assert store.get_source_event("phone_app", "evtR") is not None

    # Correct job_id deletes, durably.
    store.release_source_event("phone_app", "evtR", "jobR")
    assert store.get_source_event("phone_app", "evtR") is None
    store2 = IdempotencyStore(db_path=db_path)
    assert store2.get_source_event("phone_app", "evtR") is None
