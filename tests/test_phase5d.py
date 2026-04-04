"""Phase 5D tests — modify/escalate overrides and modified result lineage."""

from __future__ import annotations

import hashlib
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

# Add orchestrator to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from job_manager import JobManager, RESULTS_DIR
from models import Job, JobStatus
from test_helpers import MockAuditClient


# Sentinel object to enable the direct-emission demotion path in JobManager
_DEMOTION_ENABLED = object()


def _make_job(
    job_id: str = "job-001",
    status: str = JobStatus.submitted.value,
    raw_input: str = "What is the capital of France?",
    input_modality: str = "text",
    device: str = "phone",
    governing_capability_id: str | None = "route.cloud.claude",
    request_category: str | None = "quick_lookup",
    routing_recommendation: str | None = "cloud",
    candidate_models: list[str] | None = None,
    result: str | None = None,
    result_id: str | None = None,
    parent_job_id: str | None = None,
    last_failing_capability_id: str | None = None,
) -> Job:
    """Create a Job in a specific state for testing."""
    return Job(
        job_id=job_id,
        raw_input=raw_input,
        input_modality=input_modality,
        device=device,
        status=status,
        created_at="2026-04-03T00:00:00.000000Z",
        governing_capability_id=governing_capability_id,
        request_category=request_category,
        routing_recommendation=routing_recommendation,
        candidate_models=candidate_models or ["claude-sonnet-4-20250514"],
        result=result,
        result_id=result_id,
        parent_job_id=parent_job_id,
        last_failing_capability_id=last_failing_capability_id,
    )


def _make_manager(audit: MockAuditClient, demotion_engine=None) -> JobManager:
    """Create a JobManager with a mock audit client, no background worker."""
    return JobManager(audit_client=audit, demotion_engine=demotion_engine)


# ---------- a. Modify on response_received job ----------


@pytest.mark.asyncio
async def test_modify_response_received_job():
    """Modify a job in 'response_received' state -> returns 'modified' with modified_result_id.
    Three durable events: human.override, human.reviewed, job.delivered.
    Job result updated and status is 'delivered'."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.response_received.value,
        result="Original model response",
        result_id="orig-result-001",
    )
    jm._jobs[job.job_id] = job

    with patch("job_manager._write_result") as mock_write:
        result = await jm.override_job(
            job_id=job.job_id,
            override_type="modify",
            target="response",
            detail="Fixed grammar",
            device="phone",
            modified_result="Corrected model response",
        )

    assert result["status"] == "modified"
    assert result["job_id"] == job.job_id
    assert "modified_result_id" in result

    # Job result updated
    assert job.result == "Corrected model response"
    assert job.status == JobStatus.delivered.value
    assert job.override_type == "modify"

    # Three durable events: human.override, human.reviewed, job.delivered
    assert len(audit.events) == 3
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[0]["payload"]["override_type"] == "modify"
    assert audit.events[0]["source"] == "human"
    assert audit.events[1]["event_type"] == "human.reviewed"
    assert audit.events[1]["payload"]["decision"] == "modified"
    assert audit.events[1]["source"] == "human"
    assert audit.events[2]["event_type"] == "job.delivered"


# ---------- b. Modify on delivered job ----------


@pytest.mark.asyncio
async def test_modify_delivered_job():
    """Modify a 'delivered' job -> returns 'modified'. Two durable events: human.override,
    human.reviewed. No second job.delivered event. Status stays 'delivered'."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.delivered.value,
        result="Original delivered response",
        result_id="orig-result-002",
    )
    job.delivered_at = "2026-04-03T00:01:00.000000Z"
    jm._jobs[job.job_id] = job

    with patch("job_manager._write_result"):
        result = await jm.override_job(
            job_id=job.job_id,
            override_type="modify",
            target="response",
            detail="Post-delivery edit",
            device="watch",
            modified_result="Edited after delivery",
        )

    assert result["status"] == "modified"
    assert result["job_id"] == job.job_id
    assert "modified_result_id" in result

    # Job result updated, status stays delivered
    assert job.result == "Edited after delivery"
    assert job.status == JobStatus.delivered.value
    assert job.override_type == "modify"

    # Two durable events: human.override, human.reviewed (no job.delivered)
    assert len(audit.events) == 2
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[1]["event_type"] == "human.reviewed"
    # No job.delivered event
    delivered_events = audit.get_events_by_type("job.delivered")
    assert len(delivered_events) == 0


# ---------- c. Modify without modified_result text ----------


@pytest.mark.asyncio
async def test_modify_without_modified_result():
    """Modify without modified_result text -> error 'modified_result_required'."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.response_received.value,
        result="Some result",
        result_id="orig-result-003",
    )
    jm._jobs[job.job_id] = job

    # None
    result = await jm.override_job(
        job_id=job.job_id,
        override_type="modify",
        target="response",
        detail="Edit",
        device="phone",
        modified_result=None,
    )
    assert result["status"] == "error"
    assert result["reason"] == "modified_result_required"

    # Empty string
    result2 = await jm.override_job(
        job_id=job.job_id,
        override_type="modify",
        target="response",
        detail="Edit",
        device="phone",
        modified_result="",
    )
    assert result2["status"] == "error"
    assert result2["reason"] == "modified_result_required"

    # No events emitted
    assert len(audit.events) == 0


# ---------- d. Modify on pre-response job ----------


@pytest.mark.asyncio
async def test_modify_pre_response_job():
    """Modify on a pre-response job (dispatched, no result) -> error 'no_result_to_modify'."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.dispatched.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="modify",
        target="response",
        detail="Edit attempt",
        device="phone",
        modified_result="New text",
    )

    assert result["status"] == "error"
    assert result["reason"] == "no_result_to_modify"
    assert len(audit.events) == 0


# ---------- e. Modified result lineage ----------


@pytest.mark.asyncio
async def test_modified_result_lineage():
    """Verify the human.reviewed event payload has correct lineage fields."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    original_result_id = "orig-result-lineage-001"
    job = _make_job(
        status=JobStatus.response_received.value,
        result="Original text",
        result_id=original_result_id,
    )
    jm._jobs[job.job_id] = job

    modified_text = "Modified text with corrections"

    with patch("job_manager._write_result"):
        result = await jm.override_job(
            job_id=job.job_id,
            override_type="modify",
            target="response",
            detail="Grammar fix",
            device="phone",
            modified_result=modified_text,
        )

    # Find the human.reviewed event
    reviewed_events = audit.get_events_by_type("human.reviewed")
    assert len(reviewed_events) == 1
    payload = reviewed_events[0]["payload"]

    # derived_from_result_id matches original
    assert payload["derived_from_result_id"] == original_result_id

    # modified_result_id is a new UUIDv7 (non-empty, different from original)
    assert payload["modified_result_id"] is not None
    assert payload["modified_result_id"] != original_result_id
    assert payload["modified_result_id"] == result["modified_result_id"]

    # modified_result_hash is SHA-256 of the modified text
    expected_hash = hashlib.sha256(modified_text.encode("utf-8")).hexdigest()
    assert payload["modified_result_hash"] == expected_hash


# ---------- f. Both results in store ----------


@pytest.mark.asyncio
async def test_both_results_in_store():
    """After modify, _write_result is called for the modified result.
    The original was written during dispatch (tested via result_id on job)."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.response_received.value,
        result="Original response",
        result_id="orig-store-001",
    )
    jm._jobs[job.job_id] = job

    with patch("job_manager._write_result") as mock_write:
        result = await jm.override_job(
            job_id=job.job_id,
            override_type="modify",
            target="response",
            detail="Fix typo",
            device="phone",
            modified_result="Fixed response",
        )

    # _write_result was called once for the modified result
    mock_write.assert_called_once()
    call_args = mock_write.call_args
    assert call_args[0][0] == result["modified_result_id"]
    assert call_args[0][1] == "Fixed response"

    # The original result_id was set on the job (would have been written during dispatch)
    assert job.result_id is not None


# ---------- g. Escalate on response_received job ----------


@pytest.mark.asyncio
async def test_escalate_response_received_job():
    """Escalate on response_received job -> 'escalated' with successor_job_id.
    Original revoked. Events: human.override, job.classified (successor), job.revoked."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.response_received.value,
        result="Model response",
        result_id="orig-esc-001",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="escalate",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    assert result["status"] == "escalated"
    assert result["job_id"] == job.job_id
    assert "successor_job_id" in result

    # Original job is revoked
    assert job.status == JobStatus.revoked.value
    assert job.revoked_at is not None
    assert job.override_type == "escalate"

    # Successor exists with correct linkage
    successor = jm.get_job(result["successor_job_id"])
    assert successor is not None
    assert successor.parent_job_id == job.job_id
    assert successor.governing_capability_id == "route.cloud.openai"
    assert successor.status == JobStatus.classified.value

    # Events: human.override, job.classified (successor), job.revoked
    assert len(audit.events) == 3
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[0]["payload"]["override_type"] == "escalate"
    assert audit.events[1]["event_type"] == "job.classified"
    assert audit.events[1]["job_id"] == successor.job_id
    assert audit.events[2]["event_type"] == "job.revoked"
    assert audit.events[2]["payload"]["reason"] == "escalation_supersede"
    assert audit.events[2]["payload"]["successor_job_id"] == successor.job_id
    assert audit.events[2]["payload"]["override_source_event_id"] == audit.events[0]["source_event_id"]


# ---------- h. Escalate on delivered job ----------


@pytest.mark.asyncio
async def test_escalate_delivered_job():
    """Escalate on delivered job -> 'escalated', original revoked, successor spawned."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.delivered.value,
        result="Delivered response",
        result_id="orig-esc-002",
    )
    job.delivered_at = "2026-04-03T00:01:00.000000Z"
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="escalate",
        target="routing",
        detail="route.cloud.gemini",
        device="watch",
    )

    assert result["status"] == "escalated"
    assert "successor_job_id" in result

    # Original revoked
    assert job.status == JobStatus.revoked.value
    assert job.revoked_at is not None
    assert job.override_type == "escalate"

    # Successor spawned
    successor = jm.get_job(result["successor_job_id"])
    assert successor is not None
    assert successor.parent_job_id == job.job_id
    assert successor.governing_capability_id == "route.cloud.gemini"

    # Events: human.override, job.classified (successor), job.revoked
    assert len(audit.events) == 3
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[1]["event_type"] == "job.classified"
    assert audit.events[2]["event_type"] == "job.revoked"


# ---------- i. Escalate on dispatched job (pre-response) ----------


@pytest.mark.asyncio
async def test_escalate_dispatched_job():
    """Escalate on pre-response (dispatched) job -> still works, revokes original, spawns successor."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.dispatched.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="escalate",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    assert result["status"] == "escalated"
    assert "successor_job_id" in result

    # Original revoked
    assert job.status == JobStatus.revoked.value
    assert job.override_type == "escalate"

    # Successor spawned
    successor = jm.get_job(result["successor_job_id"])
    assert successor is not None
    assert successor.parent_job_id == job.job_id


# ---------- j. Escalate does NOT trigger demotion ----------


@pytest.mark.asyncio
async def test_escalate_does_not_trigger_demotion():
    """Escalate is not in CONDITIONAL_DEMOTION_TYPES — no wal.demoted event."""
    audit = MockAuditClient()
    jm = _make_manager(audit, demotion_engine=_DEMOTION_ENABLED)

    job = _make_job(status=JobStatus.response_received.value)
    jm._jobs[job.job_id] = job

    await jm.override_job(
        job_id=job.job_id,
        override_type="escalate",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 0


# ---------- k. Modify does NOT trigger demotion ----------


@pytest.mark.asyncio
async def test_modify_does_not_trigger_demotion():
    """Modify is not in CONDITIONAL_DEMOTION_TYPES — no wal.demoted event."""
    audit = MockAuditClient()
    jm = _make_manager(audit, demotion_engine=_DEMOTION_ENABLED)

    job = _make_job(
        status=JobStatus.response_received.value,
        result="Original",
        result_id="orig-demod-001",
    )
    jm._jobs[job.job_id] = job

    with patch("job_manager._write_result"):
        await jm.override_job(
            job_id=job.job_id,
            override_type="modify",
            target="response",
            detail="Fix",
            device="phone",
            modified_result="Fixed",
        )

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 0


# ---------- l. No demotion on modify/escalate (explicit negative) ----------


@pytest.mark.asyncio
async def test_no_demotion_on_modify_and_escalate():
    """Pass a truthy demotion_engine, do modify and escalate — zero wal.demoted for both."""
    audit = MockAuditClient()
    jm = _make_manager(audit, demotion_engine=_DEMOTION_ENABLED)

    # Modify
    job_modify = _make_job(
        job_id="job-mod-demod",
        status=JobStatus.response_received.value,
        result="Original mod",
        result_id="orig-demod-mod",
    )
    jm._jobs[job_modify.job_id] = job_modify

    with patch("job_manager._write_result"):
        await jm.override_job(
            job_id=job_modify.job_id,
            override_type="modify",
            target="response",
            detail="Edit",
            device="phone",
            modified_result="Edited",
        )

    demoted_after_modify = audit.get_events_by_type("wal.demoted")
    assert len(demoted_after_modify) == 0

    audit.clear()

    # Escalate
    job_escalate = _make_job(
        job_id="job-esc-demod",
        status=JobStatus.dispatched.value,
    )
    jm._jobs[job_escalate.job_id] = job_escalate

    await jm.override_job(
        job_id=job_escalate.job_id,
        override_type="escalate",
        target="routing",
        detail="route.cloud.openai",
        device="watch",
    )

    demoted_after_escalate = audit.get_events_by_type("wal.demoted")
    assert len(demoted_after_escalate) == 0
