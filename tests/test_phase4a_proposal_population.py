"""Phase 4A.2.b proposal-population tests.

Drives the JobManager pipeline through the result-holding review-gate path
and asserts:
    - delivery_hold == True transitions the job to proposal_ready and does
      not silently advance to delivered.
    - hold_type == "on_accept" with a result transitions to proposal_ready.
    - pre_action and cost_approval holds do NOT become proposal_ready.
    - The durable job.proposal_ready event is emitted with the locked
      payload (proposal_id, result_id, response_hash, proposed_by,
      governing_capability_id, confidence, auto_accept_at, hold_reason).
    - JobManager.get_proposal returns a populated Proposal whose
      proposed_by is the producing model identifier and whose
      auto_accept_at is None for v1.
    - Non-held jobs still reach delivered and get_proposal returns None.
    - The new optional Job fields survive the JSON/blob persistence
      roundtrip.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import job_manager as jm_module
from events import event_wal_permission_check
from job_manager import JobManager
from models import Job, JobStatus
from permission_checker import PermissionResult
from test_helpers import MockAuditClient


# ---------- Test helpers ----------


class _FakeLocalResponse:
    def __init__(self, text: str = "Paris."):
        self.text = text
        self.token_count_in = 4
        self.token_count_out = 5


class _MockPermissionChecker:
    """Minimal permission_checker stub returning a fixed PermissionResult.

    Mirrors the real contract: emit a durable wal.permission_check via the
    audit client, attach source_event_id to the returned result.
    """

    def __init__(
        self,
        *,
        result: str = "allowed",
        delivery_hold: bool = False,
        hold_type: str | None = None,
        block_reason: str | None = None,
        audit_client: MockAuditClient | None = None,
    ):
        self._result = result
        self._delivery_hold = delivery_hold
        self._hold_type = hold_type
        self._block_reason = block_reason
        self._audit = audit_client

    async def check_permission(
        self,
        capability_id: str,
        action: str,
        job_ctx: Any,
        dependency_mode: str | None = None,
    ) -> PermissionResult:
        event = event_wal_permission_check(
            job_id=job_ctx.job_id,
            capability_id=capability_id,
            requested_action=action,
            current_level=0,
            required_level=0,
            result=self._result,
            delivery_hold=self._delivery_hold,
            hold_type=self._hold_type,
            block_reason=self._block_reason,
            wal_level=0,
        )
        if self._audit is not None:
            await self._audit.emit_durable(event)
        return PermissionResult(
            result=self._result,
            delivery_hold=self._delivery_hold,
            hold_type=self._hold_type,
            block_reason=self._block_reason,
            capability_id=capability_id,
            requested_action=action,
            current_level=0,
            required_level=0,
            source_event_id=event["source_event_id"],
        )


def _classified_local_job(
    *,
    job_id: str = "job-prop-001",
    capability_id: str = "route.local",
    candidate_models: list[str] | None = None,
    confidence: float | None = 0.92,
) -> Job:
    """A Job already in 'classified' state for the local route.

    Skipping classification lets _run_pipeline take the successor-style
    branch and avoid Ollama I/O while still exercising dispatch +
    response + the proposal-ready transition.
    """
    return Job(
        job_id=job_id,
        raw_input="What is the capital of France?",
        input_modality="text",
        device="phone",
        status=JobStatus.classified.value,
        created_at="2026-04-26T12:00:00.000000Z",
        classified_at="2026-04-26T12:00:00.000000Z",
        request_category="quick_lookup",
        routing_recommendation="local",
        candidate_models=candidate_models or ["llama3.1:8b"],
        governing_capability_id=capability_id,
        confidence=confidence,
    )


def _make_manager(
    audit: MockAuditClient,
    *,
    permission_checker=None,
    db_path: str | None = None,
) -> JobManager:
    return JobManager(
        audit_client=audit,
        permission_checker=permission_checker,
        db_path=db_path,
    )


def _stub_local_response(monkeypatch, response_text: str = "Paris."):
    async def _fake(_prompt: str):
        return _FakeLocalResponse(response_text)

    monkeypatch.setattr(jm_module, "generate_local_response", _fake)


def _create_jobs_db(db_path: str) -> None:
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


# ---------- delivery_hold == True -> proposal_ready ----------


@pytest.mark.asyncio
async def test_delivery_hold_transitions_to_proposal_ready(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job()
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    assert job.status == JobStatus.proposal_ready.value
    assert job.delivered_at is None
    assert audit.get_events_by_type("job.delivered") == []


@pytest.mark.asyncio
async def test_proposal_ready_emits_durable_job_proposal_ready_event(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job()
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    events = audit.get_events_by_type("job.proposal_ready")
    assert len(events) == 1
    evt = events[0]
    assert evt["job_id"] == job.job_id
    assert evt["durability"] == "durable"
    assert evt["capability_id"] == job.governing_capability_id


@pytest.mark.asyncio
async def test_proposal_ready_payload_includes_required_fields(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job(confidence=0.77)
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    evt = audit.get_events_by_type("job.proposal_ready")[0]
    payload = evt["payload"]
    for key in (
        "proposal_id",
        "result_id",
        "response_hash",
        "proposed_by",
        "governing_capability_id",
        "confidence",
        "auto_accept_at",
        "hold_reason",
    ):
        assert key in payload, f"missing payload key: {key}"

    assert payload["proposal_id"] == job.proposal_id
    assert payload["result_id"] == job.result_id
    assert payload["response_hash"] == job.response_hash
    assert payload["proposed_by"] == "llama3.1:8b"
    assert payload["governing_capability_id"] == job.governing_capability_id
    assert payload["confidence"] == 0.77
    assert payload["auto_accept_at"] is None
    assert payload["hold_reason"] == "pre_delivery"


@pytest.mark.asyncio
async def test_delivery_hold_yields_pre_delivery_hold_reason(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job()
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    evt = audit.get_events_by_type("job.proposal_ready")[0]
    assert evt["payload"]["hold_reason"] == "pre_delivery"


# ---------- on_accept hold ----------


@pytest.mark.asyncio
async def test_on_accept_hold_transitions_to_proposal_ready_with_hold_reason(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="held",
        hold_type="on_accept",
        delivery_hold=False,
        audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job(job_id="job-prop-002")
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    assert job.status == JobStatus.proposal_ready.value
    evt = audit.get_events_by_type("job.proposal_ready")[0]
    assert evt["payload"]["hold_reason"] == "on_accept"


# ---------- pre_action / cost_approval are NOT triggers ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_type", ["pre_action", "cost_approval"])
async def test_pre_action_and_cost_approval_holds_do_not_become_proposal_ready(
    monkeypatch, hold_type: str,
):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="held",
        hold_type=hold_type,
        delivery_hold=False,
        audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job(job_id=f"job-prop-{hold_type}")
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    assert audit.get_events_by_type("job.proposal_ready") == []
    assert job.status == JobStatus.delivered.value


# ---------- get_proposal / Proposal shape ----------


@pytest.mark.asyncio
async def test_get_proposal_returns_proposal_for_proposal_ready_job(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job(confidence=0.81)
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    proposal = jm.get_proposal(job.job_id)
    assert proposal is not None
    assert proposal.proposal_id == job.proposal_id
    assert proposal.job_id == job.job_id
    assert proposal.result_id == job.result_id
    assert proposal.response_hash == job.response_hash
    assert proposal.proposed_by == "llama3.1:8b"
    assert proposal.governing_capability_id == "route.local"
    assert proposal.confidence == 0.81
    assert proposal.auto_accept_at is None


@pytest.mark.asyncio
async def test_proposed_by_is_producing_model_identifier(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job(candidate_models=["llama3.1:8b"])
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    proposal = jm.get_proposal(job.job_id)
    assert proposal is not None
    assert proposal.proposed_by == "llama3.1:8b"
    evt = audit.get_events_by_type("job.proposal_ready")[0]
    assert evt["payload"]["proposed_by"] == "llama3.1:8b"


@pytest.mark.asyncio
async def test_proposal_auto_accept_at_is_none(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=True, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job()
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    proposal = jm.get_proposal(job.job_id)
    assert proposal is not None
    assert proposal.auto_accept_at is None


# ---------- Non-held still delivers ----------


@pytest.mark.asyncio
async def test_non_held_job_still_reaches_delivered_and_get_proposal_returns_none(monkeypatch):
    audit = MockAuditClient()
    pc = _MockPermissionChecker(
        result="allowed", delivery_hold=False, audit_client=audit,
    )
    jm = _make_manager(audit, permission_checker=pc)
    _stub_local_response(monkeypatch)

    job = _classified_local_job(job_id="job-prop-deliver")
    jm._jobs[job.job_id] = job

    await jm._run_pipeline(job.job_id)

    assert job.status == JobStatus.delivered.value
    assert audit.get_events_by_type("job.proposal_ready") == []
    assert audit.get_events_by_type("job.delivered") != []
    assert jm.get_proposal(job.job_id) is None


def test_get_proposal_returns_none_for_unknown_job():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    assert jm.get_proposal("does-not-exist") is None


def test_get_proposal_returns_none_for_non_proposal_ready_status():
    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _classified_local_job()
    jm._jobs[job.job_id] = job
    assert jm.get_proposal(job.job_id) is None


# ---------- Persistence survival of new Job fields ----------


def test_new_job_fields_survive_persistence_roundtrip(tmp_path):
    db_path = str(tmp_path / "jobs.db")
    _create_jobs_db(db_path)

    audit = MockAuditClient()
    mgr1 = JobManager(audit_client=audit, db_path=db_path)
    job = Job(
        job_id="job-persist-001",
        raw_input="hello",
        input_modality="text",
        device="phone",
        status=JobStatus.proposal_ready.value,
        created_at="2026-04-26T12:00:00.000000Z",
        request_category="quick_lookup",
        routing_recommendation="local",
        candidate_models=["llama3.1:8b"],
        governing_capability_id="route.local",
        result="42",
        result_id="res-001",
        response_hash="resp-hash-abc",
        confidence=0.91,
        proposal_id="prop-001",
    )
    mgr1._jobs[job.job_id] = job
    mgr1._persist_job(job)

    mgr2 = JobManager(audit_client=MockAuditClient(), db_path=db_path)
    recovered = mgr2.get_job(job.job_id)
    assert recovered is not None
    assert recovered.proposal_id == "prop-001"
    assert recovered.response_hash == "resp-hash-abc"
    assert recovered.confidence == 0.91
    assert recovered.status == JobStatus.proposal_ready.value

    proposal = mgr2.get_proposal(job.job_id)
    assert proposal is not None
    assert proposal.proposal_id == "prop-001"
    assert proposal.confidence == 0.91
    assert proposal.proposed_by == "llama3.1:8b"
    assert proposal.governing_capability_id == "route.local"
    assert proposal.auto_accept_at is None
