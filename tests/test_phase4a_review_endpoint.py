"""Phase 4A.2.d POST /jobs/{job_id}/review handler tests.

Exercises JobManager.review_job semantics defined by the Phase 4A.2.c
amendment "Define review handler conflict and serialization semantics" and
the Phase 4A.2.d amendment "Define review implementation mechanics".

Coverage:
    - Decision behavior: approve, edit, reject, defer, decline_to_act.
    - Handler-side modified_result 422 rules.
    - Wrong-status / stale-decision 409 with locked body shape.
    - review_idem idempotency: replay, conflicting payload, fresh-key reuse.
    - Persistence: review_idem records survive IdempotencyStore restart.
    - First-writer ordering: review_decision and override_type set BEFORE
      durable emits.
    - Audit event vocabulary and payloads (human.reviewed, job.delivered,
      job.failed, job.closed_no_action).
    - HTTP route surface via FastAPI TestClient is exercised separately to
      confirm 422 / 409 / 200 mappings.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from idempotency_store import IdempotencyStore
from job_manager import JobManager
from models import Job, JobStatus, ReviewDecision
from test_helpers import MockAuditClient


# ---------- Test scaffolding ----------


def _make_proposal_ready_job(
    *,
    job_id: str = "job-rev-001",
    result_id: str = "res-orig-001",
    response_hash: str = "hash-orig-001",
    capability_id: str = "route.local",
    candidate_models: list[str] | None = None,
    confidence: float | None = 0.91,
    proposal_id: str = "prop-001",
    result: str = "Paris.",
) -> Job:
    """Build a Job already populated as proposal_ready."""
    return Job(
        job_id=job_id,
        raw_input="What is the capital of France?",
        input_modality="text",
        device="phone",
        status=JobStatus.proposal_ready.value,
        created_at="2026-04-26T12:00:00.000000Z",
        request_category="quick_lookup",
        routing_recommendation="local",
        candidate_models=candidate_models or ["llama3.1:8b"],
        governing_capability_id=capability_id,
        result=result,
        result_id=result_id,
        response_hash=response_hash,
        confidence=confidence,
        proposal_id=proposal_id,
    )


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


def _valid_review_kwargs(job: Job, **overrides) -> dict:
    """Default kwargs matching the seeded proposal-ready job."""
    base = {
        "job_id": job.job_id,
        "decision": ReviewDecision.approve.value,
        "result_id": job.result_id,
        "response_hash": job.response_hash,
        "decision_idempotency_key": "rev-key-001",
        "modified_result": None,
    }
    base.update(overrides)
    return base


class _StateObservingAudit(MockAuditClient):
    """Audit client that snapshots target-job guard state at emit time."""

    def __init__(self, jobs: dict, job_id: str):
        super().__init__()
        self._jobs = jobs
        self._job_id = job_id
        self.review_decision_at_emit: list[tuple[str, str | None]] = []
        self.override_type_at_emit: list[tuple[str, str | None]] = []

    async def emit_durable(self, event: dict) -> bool:
        job = self._jobs.get(self._job_id)
        rd = job.review_decision if job else None
        ot = job.override_type if job else None
        self.review_decision_at_emit.append((event["event_type"], rd))
        self.override_type_at_emit.append((event["event_type"], ot))
        return await super().emit_durable(event)


# ---------- 1. approve ----------


@pytest.mark.asyncio
async def test_approve_transitions_to_delivered():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(**_valid_review_kwargs(job))

    assert status == 200
    assert body["status"] == "delivered"
    assert body["decision"] == "approve"
    assert job.status == JobStatus.delivered.value
    assert job.delivered_at is not None
    assert job.review_decision == ReviewDecision.approve.value

    reviewed = audit.get_events_by_type("human.reviewed")
    assert len(reviewed) == 1
    assert reviewed[0]["payload"]["decision"] == "approve"
    delivered = audit.get_events_by_type("job.delivered")
    assert len(delivered) == 1


# ---------- 2. edit ----------


@pytest.mark.asyncio
async def test_edit_requires_modified_result_returns_422():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(job, decision="edit", modified_result=None),
    )
    assert status == 422
    assert body["error"] == "modified_result_required"
    assert job.status == JobStatus.proposal_ready.value
    assert job.review_decision is None
    assert audit.get_events_by_type("human.reviewed") == []


@pytest.mark.asyncio
async def test_edit_transitions_to_delivered_with_updated_authoritative_result():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    original_result_id = job.result_id
    original_response_hash = job.response_hash
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(
            job, decision="edit", modified_result="Paris (the capital)."
        ),
    )

    assert status == 200
    assert body["status"] == "delivered"
    assert body["decision"] == "edit"
    assert body["result_id"] != original_result_id
    assert body["response_hash"] != original_response_hash
    assert body["derived_from_result_id"] == original_result_id

    assert job.status == JobStatus.delivered.value
    assert job.result == "Paris (the capital)."
    assert job.result_id == body["result_id"]
    assert job.response_hash == body["response_hash"]
    assert job.review_decision == ReviewDecision.edit.value


# ---------- 3. reject ----------


@pytest.mark.asyncio
async def test_reject_transitions_to_failed():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(job, decision="reject"),
    )

    assert status == 200
    assert body["status"] == "failed"
    assert body["decision"] == "reject"
    assert job.status == JobStatus.failed.value
    assert job.error == "review_reject"
    assert job.review_decision == ReviewDecision.reject.value

    failed_events = audit.get_events_by_type("job.failed")
    assert len(failed_events) == 1
    assert failed_events[0]["payload"]["error_class"] == "review_reject"
    reviewed = audit.get_events_by_type("human.reviewed")
    assert reviewed[0]["payload"]["decision"] == "reject"


# ---------- 4. defer ----------


@pytest.mark.asyncio
async def test_defer_leaves_job_in_proposal_ready_and_permits_later_review():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(
            job, decision="defer", decision_idempotency_key="defer-key-1",
        ),
    )

    assert status == 200
    assert body["status"] == "deferred"
    assert body["decision"] == "defer"
    assert job.status == JobStatus.proposal_ready.value
    assert job.review_decision is None  # defer must NOT set the guard

    # Fresh key on a still-proposal_ready job is permitted.
    status2, body2 = await jm.review_job(
        **_valid_review_kwargs(
            job, decision="approve", decision_idempotency_key="approve-key-2",
        ),
    )
    assert status2 == 200
    assert body2["status"] == "delivered"
    assert job.status == JobStatus.delivered.value


# ---------- 5. decline_to_act ----------


@pytest.mark.asyncio
async def test_decline_to_act_transitions_to_closed_no_action_and_emits_event():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(
            job, decision="decline_to_act", decision_idempotency_key="dec-key-1",
        ),
    )

    assert status == 200
    assert body["status"] == "closed_no_action"
    assert body["decision"] == "decline_to_act"
    assert job.status == JobStatus.closed_no_action.value
    assert job.review_decision == ReviewDecision.decline_to_act.value

    closed = audit.get_events_by_type("job.closed_no_action")
    assert len(closed) == 1
    payload = closed[0]["payload"]
    for key in (
        "result_id",
        "response_hash",
        "review_decision",
        "decision_idempotency_key",
        "governing_capability_id",
        "reason",
    ):
        assert key in payload
    assert payload["review_decision"] == "decline_to_act"
    assert payload["reason"] == "decline_to_act"
    assert payload["decision_idempotency_key"] == "dec-key-1"


# ---------- 6. modified_result handler validation ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    ["approve", "reject", "defer", "decline_to_act"],
)
async def test_non_edit_with_modified_result_returns_422(decision: str):
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(
            job,
            decision=decision,
            modified_result="should not be allowed",
            decision_idempotency_key=f"key-{decision}",
        ),
    )
    assert status == 422
    assert body["error"] == "modified_result_not_allowed"
    assert job.status == JobStatus.proposal_ready.value
    assert audit.get_events_by_type("human.reviewed") == []


# ---------- 7. wrong-status 409 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_value",
    [
        JobStatus.delivered.value,
        JobStatus.failed.value,
        JobStatus.revoked.value,
        JobStatus.closed_no_action.value,
        JobStatus.classified.value,
    ],
)
async def test_review_on_non_proposal_ready_returns_409_locked_body(status_value):
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    job.status = status_value
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(**_valid_review_kwargs(job))
    assert status == 409
    for key in (
        "error",
        "current_status",
        "current_result_id",
        "current_response_hash",
        "message",
    ):
        assert key in body
    assert body["current_status"] == status_value


# ---------- 8. stale result_id 409 ----------


@pytest.mark.asyncio
async def test_stale_result_id_returns_409_locked_body():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(job, result_id="stale-result-id"),
    )
    assert status == 409
    assert body["error"] == "stale_decision"
    assert body["current_result_id"] == job.result_id
    assert body["current_response_hash"] == job.response_hash
    assert body["current_status"] == JobStatus.proposal_ready.value
    assert audit.get_events_by_type("human.reviewed") == []


# ---------- 9. stale response_hash 409 ----------


@pytest.mark.asyncio
async def test_stale_response_hash_returns_409_locked_body():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(
        **_valid_review_kwargs(job, response_hash="stale-hash"),
    )
    assert status == 409
    assert body["error"] == "stale_decision"
    assert body["current_response_hash"] == job.response_hash


# ---------- 10. override_type already set causes 409 ----------


@pytest.mark.asyncio
async def test_review_when_override_type_already_set_returns_409():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    job.override_type = "cancel"
    jm._jobs[job.job_id] = job

    status, body = await jm.review_job(**_valid_review_kwargs(job))
    assert status == 409
    for key in (
        "error",
        "current_status",
        "current_result_id",
        "current_response_hash",
        "message",
    ):
        assert key in body


# ---------- 11. idempotency: same key + same payload replays ----------


@pytest.mark.asyncio
async def test_same_key_same_payload_replays_original_outcome():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    kwargs = _valid_review_kwargs(job, decision_idempotency_key="rep-key-1")
    status1, body1 = await jm.review_job(**kwargs)
    assert status1 == 200

    # First-writer guard set, status delivered. Replay must short-circuit
    # to the stored outcome WITHOUT re-checking proposal_ready precondition
    # (which would now fail because the job is delivered).
    status2, body2 = await jm.review_job(**kwargs)
    assert status2 == 200
    assert body2 == body1


# ---------- 12. replay does not emit duplicate human.reviewed ----------


@pytest.mark.asyncio
async def test_replay_does_not_emit_duplicate_human_reviewed():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    kwargs = _valid_review_kwargs(job, decision_idempotency_key="rep-key-2")
    await jm.review_job(**kwargs)
    audit_events_after_first = list(audit.events)

    await jm.review_job(**kwargs)
    assert audit.events == audit_events_after_first
    assert len(audit.get_events_by_type("human.reviewed")) == 1
    assert len(audit.get_events_by_type("job.delivered")) == 1


# ---------- 13. same key + different payload returns 409 ----------


@pytest.mark.asyncio
async def test_same_key_different_payload_returns_409():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    key = "conflict-key-1"
    status1, _ = await jm.review_job(
        **_valid_review_kwargs(job, decision_idempotency_key=key),
    )
    assert status1 == 200

    audit_events_after_first = list(audit.events)

    # Reuse the same key with a different decision payload.
    status2, body2 = await jm.review_job(
        **_valid_review_kwargs(
            job,
            decision="reject",
            decision_idempotency_key=key,
        ),
    )
    assert status2 == 409
    assert body2["error"] == "decision_idempotency_key_conflict"
    # No new durable events.
    assert audit.events == audit_events_after_first


# ---------- 14. different key + same payload after terminal: wrong-status 409 ----------


@pytest.mark.asyncio
async def test_fresh_key_after_terminal_decision_returns_wrong_status_409():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    await jm.review_job(
        **_valid_review_kwargs(job, decision_idempotency_key="first-key"),
    )
    assert job.status == JobStatus.delivered.value

    status, body = await jm.review_job(
        **_valid_review_kwargs(job, decision_idempotency_key="second-key"),
    )
    assert status == 409
    assert body["current_status"] == JobStatus.delivered.value


# ---------- 15. review_idem persistence survival ----------


def test_review_idem_record_survives_idempotency_store_restart(tmp_path):
    db_path = str(tmp_path / "review.db")
    _create_db(db_path)

    store1 = IdempotencyStore(db_path=db_path)
    payload_identity = {
        "job_id": "job-x",
        "decision": "approve",
        "result_id": "res-1",
        "response_hash": "hash-1",
        "modified_result": None,
        "decision_idempotency_key": "persist-key-1",
    }
    outcome = {
        "status_code": 200,
        "body": {
            "status": "delivered",
            "job_id": "job-x",
            "decision": "approve",
            "result_id": "res-1",
            "response_hash": "hash-1",
        },
    }
    store1.store_review_outcome(
        "persist-key-1", payload_identity, outcome, applied=True,
    )

    store2 = IdempotencyStore(db_path=db_path)
    rec = store2.get_review_outcome("persist-key-1")
    assert rec is not None
    assert rec.payload_identity == payload_identity
    assert rec.outcome == outcome
    assert rec.applied is True


# ---------- 16. review sets review_decision before durable emits ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,expected_terminal_event,expected_guard",
    [
        ("approve", "job.delivered", "approve"),
        ("edit", "job.delivered", "edit"),
        ("reject", "job.failed", "reject"),
        ("decline_to_act", "job.closed_no_action", "decline_to_act"),
    ],
)
async def test_review_decision_set_before_durable_emit(
    decision, expected_terminal_event, expected_guard,
):
    job = _make_proposal_ready_job(job_id=f"job-order-{decision}")
    jobs: dict = {job.job_id: job}
    audit = _StateObservingAudit(jobs, job.job_id)
    jm = JobManager(audit_client=audit)
    jm._jobs = jobs  # share dict so audit observes the same Job object

    kwargs = _valid_review_kwargs(
        job,
        decision=decision,
        decision_idempotency_key=f"order-key-{decision}",
        modified_result="x" if decision == "edit" else None,
    )
    await jm.review_job(**kwargs)

    # human.reviewed must be emitted only after review_decision is set.
    snap = dict(audit.review_decision_at_emit)
    assert snap.get("human.reviewed") == expected_guard
    assert snap.get(expected_terminal_event) == expected_guard


# ---------- 17. override after review returns no-op ----------


@pytest.mark.asyncio
async def test_override_after_review_returns_no_op_and_does_not_mutate():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    await jm.review_job(**_valid_review_kwargs(job))
    assert job.status == JobStatus.delivered.value
    assert job.review_decision == ReviewDecision.approve.value

    audit_events_after_review = list(audit.events)
    job_status_after_review = job.status
    job_override_after_review = job.override_type

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="post-review attempt",
        device="phone",
    )
    assert result["status"] == "no_op"
    assert result["reason"] == "already_reviewed"
    # No additional durable events emitted by the override.
    assert audit.events == audit_events_after_review
    assert job.status == job_status_after_review
    assert job.override_type == job_override_after_review


# ---------- 18. override on proposal_ready sets override_type before emit ----------


@pytest.mark.asyncio
async def test_override_on_proposal_ready_sets_override_type_before_emit():
    job = _make_proposal_ready_job(job_id="job-override-order")
    jobs: dict = {job.job_id: job}
    audit = _StateObservingAudit(jobs, job.job_id)
    jm = JobManager(audit_client=audit)
    jm._jobs = jobs

    await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="pre-emit guard test",
        device="phone",
    )

    # human.override must observe override_type already set on the job.
    for evt_type, ot_at_emit in audit.override_type_at_emit:
        if evt_type == "human.override":
            assert ot_at_emit == "cancel", (
                "override_type was not set before durable human.override emit"
            )
            break
    else:
        pytest.fail("human.override event was not emitted")


# ---------- 19. human.reviewed uses ReviewDecision vocabulary exactly ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,modified",
    [
        ("approve", None),
        ("edit", "edited content"),
        ("reject", None),
        ("defer", None),
        ("decline_to_act", None),
    ],
)
async def test_human_reviewed_uses_review_decision_vocabulary_exactly(
    decision, modified,
):
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job(job_id=f"job-vocab-{decision}")
    jm._jobs[job.job_id] = job

    await jm.review_job(
        **_valid_review_kwargs(
            job,
            decision=decision,
            modified_result=modified,
            decision_idempotency_key=f"vocab-key-{decision}",
        ),
    )

    reviewed = audit.get_events_by_type("human.reviewed")
    assert len(reviewed) == 1
    # Must be the literal ReviewDecision value, not translated to legacy
    # accepted/modified/rejected/resubmitted strings.
    assert reviewed[0]["payload"]["decision"] == decision


# ---------- 20. job.closed_no_action payload ----------


@pytest.mark.asyncio
async def test_closed_no_action_payload_includes_required_fields():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job(capability_id="route.local")
    jm._jobs[job.job_id] = job

    await jm.review_job(
        **_valid_review_kwargs(
            job,
            decision="decline_to_act",
            decision_idempotency_key="closed-payload-key",
        ),
    )

    closed = audit.get_events_by_type("job.closed_no_action")[0]
    payload = closed["payload"]
    assert payload["result_id"] == job.result_id
    assert payload["response_hash"] == job.response_hash
    assert payload["review_decision"] == "decline_to_act"
    assert payload["decision_idempotency_key"] == "closed-payload-key"
    assert payload["governing_capability_id"] == "route.local"
    assert payload["reason"] == "decline_to_act"


# ---------- 21. edit human.reviewed includes modified-result lineage ----------


@pytest.mark.asyncio
async def test_edit_human_reviewed_includes_modified_result_lineage():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    original_result_id = job.result_id
    jm._jobs[job.job_id] = job

    await jm.review_job(
        **_valid_review_kwargs(
            job,
            decision="edit",
            modified_result="edited",
            decision_idempotency_key="lineage-key",
        ),
    )

    reviewed = audit.get_events_by_type("human.reviewed")[0]
    payload = reviewed["payload"]
    assert payload["modified_result_id"] is not None
    assert payload["modified_result_hash"] is not None
    assert payload["derived_from_result_id"] == original_result_id


# ---------- 22. terminal lifecycle event emission per decision ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,terminal_event,modified",
    [
        ("approve", "job.delivered", None),
        ("edit", "job.delivered", "x"),
        ("reject", "job.failed", None),
        ("decline_to_act", "job.closed_no_action", None),
    ],
)
async def test_terminal_decisions_emit_expected_lifecycle_event(
    decision, terminal_event, modified,
):
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job(job_id=f"job-lc-{decision}")
    jm._jobs[job.job_id] = job

    await jm.review_job(
        **_valid_review_kwargs(
            job,
            decision=decision,
            modified_result=modified,
            decision_idempotency_key=f"lc-key-{decision}",
        ),
    )
    assert len(audit.get_events_by_type(terminal_event)) == 1


@pytest.mark.asyncio
async def test_defer_does_not_emit_terminal_lifecycle_event():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job()
    jm._jobs[job.job_id] = job

    await jm.review_job(
        **_valid_review_kwargs(
            job, decision="defer", decision_idempotency_key="defer-no-terminal",
        ),
    )
    for terminal in ("job.delivered", "job.failed", "job.closed_no_action"):
        assert audit.get_events_by_type(terminal) == []


# ---------- HTTP route surface (smoke) ----------
# Confirms the FastAPI route correctly wires status code and JSON body via
# JSONResponse. Avoids the orchestrator lifespan by invoking the route
# function under a swapped global job_manager.


@pytest.mark.asyncio
async def test_route_returns_404_for_unknown_job():
    """If review_job returns (404, body), the route surfaces it as HTTP 404."""
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    saved = main_module.job_manager
    main_module.job_manager = jm
    try:
        from models import ReviewRequest

        req = ReviewRequest(
            decision="approve",
            result_id="x",
            response_hash="y",
            decision_idempotency_key="route-404",
        )
        resp = await main_module.review_job("missing-job", req)
        assert resp.status_code == 404
    finally:
        main_module.job_manager = saved


@pytest.mark.asyncio
async def test_route_returns_200_on_approve():
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job(job_id="job-route-approve")
    jm._jobs[job.job_id] = job

    saved = main_module.job_manager
    main_module.job_manager = jm
    try:
        from models import ReviewRequest

        req = ReviewRequest(
            decision="approve",
            result_id=job.result_id,
            response_hash=job.response_hash,
            decision_idempotency_key="route-approve-key",
        )
        resp = await main_module.review_job(job.job_id, req)
        assert resp.status_code == 200
    finally:
        main_module.job_manager = saved


@pytest.mark.asyncio
async def test_route_returns_422_on_modified_result_violation():
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job(job_id="job-route-422")
    jm._jobs[job.job_id] = job

    saved = main_module.job_manager
    main_module.job_manager = jm
    try:
        from models import ReviewRequest

        # decision=approve with modified_result -> handler-side 422
        req = ReviewRequest(
            decision="approve",
            result_id=job.result_id,
            response_hash=job.response_hash,
            decision_idempotency_key="route-422-key",
            modified_result="not allowed",
        )
        resp = await main_module.review_job(job.job_id, req)
        assert resp.status_code == 422
    finally:
        main_module.job_manager = saved


@pytest.mark.asyncio
async def test_route_returns_409_on_stale_decision():
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_proposal_ready_job(job_id="job-route-409")
    jm._jobs[job.job_id] = job

    saved = main_module.job_manager
    main_module.job_manager = jm
    try:
        from models import ReviewRequest

        req = ReviewRequest(
            decision="approve",
            result_id="stale",
            response_hash="also-stale",
            decision_idempotency_key="route-409-key",
        )
        resp = await main_module.review_job(job.job_id, req)
        assert resp.status_code == 409
    finally:
        main_module.job_manager = saved
