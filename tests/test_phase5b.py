"""Phase 5B tests — cancel/redirect override mechanics and successor jobs."""

from __future__ import annotations

import asyncio
import sys
import os
from unittest.mock import AsyncMock, patch

import pytest

# Add orchestrator to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from classifier import MODEL_MAP
from events import event_job_classified
from job_manager import JobManager
from models import Job, JobStatus
from test_helpers import MockAuditClient


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
    parent_job_id: str | None = None,
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
        parent_job_id=parent_job_id,
    )


def _make_manager(audit: MockAuditClient) -> JobManager:
    """Create a JobManager with a mock audit client, no background worker."""
    return JobManager(audit_client=audit)


# ---------- a. Cancel pre-delivery job ----------


@pytest.mark.asyncio
async def test_cancel_pre_delivery_job():
    """Cancel a job in 'dispatched' state -> status 'failed', error 'override_cancel'."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.dispatched.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="watch",
    )

    assert result["status"] == "cancelled"
    assert result["job_id"] == job.job_id

    assert job.status == JobStatus.failed.value
    assert job.error == "override_cancel"
    assert job.override_type == "cancel"

    # Two durable events: human.override then job.failed
    assert len(audit.events) == 2
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[0]["payload"]["override_type"] == "cancel"
    assert audit.events[0]["source"] == "human"
    assert audit.events[1]["event_type"] == "job.failed"
    assert audit.events[1]["payload"]["error_class"] == "override_cancel"
    assert audit.events[1]["payload"]["failing_capability_id"] == "route.cloud.claude"


# ---------- b. Cancel delivered job ----------


@pytest.mark.asyncio
async def test_cancel_delivered_job():
    """Cancel a 'delivered' job -> status 'revoked', revoked_at set."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.delivered.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="phone",
    )

    assert result["status"] == "revoked"
    assert result["job_id"] == job.job_id

    assert job.status == JobStatus.revoked.value
    assert job.revoked_at is not None
    assert job.override_type == "cancel"

    # Two durable events: human.override then job.revoked
    assert len(audit.events) == 2
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[1]["event_type"] == "job.revoked"
    assert audit.events[1]["payload"]["reason"] == "user_cancel"
    # override_source_event_id links back to the human.override event
    assert audit.events[1]["payload"]["override_source_event_id"] == audit.events[0]["source_event_id"]
    assert audit.events[1]["payload"]["successor_job_id"] is None


# ---------- c. Cancel terminal job (already failed) ----------


@pytest.mark.asyncio
async def test_cancel_terminal_job_is_noop():
    """Cancel on an already-failed job returns no_op, no events emitted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.failed.value)
    job.error = "some_previous_error"
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="watch",
    )

    assert result["status"] == "no_op"
    assert result["reason"] == "job_already_terminal"
    assert result["job_id"] == job.job_id

    # No events emitted
    assert len(audit.events) == 0

    # Job unchanged
    assert job.status == JobStatus.failed.value
    assert job.error == "some_previous_error"


# ---------- d. Redirect pre-delivery job ----------


@pytest.mark.asyncio
async def test_redirect_pre_delivery_job():
    """Redirect a 'response_received' job -> original failed, successor spawned."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.response_received.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="redirect",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    assert result["status"] == "redirected"
    assert result["job_id"] == job.job_id
    assert "successor_job_id" in result

    # Original job is failed
    assert job.status == JobStatus.failed.value
    assert job.error == "override_redirect"
    assert job.override_type == "redirect"

    # Successor exists with correct linkage
    successor = jm.get_job(result["successor_job_id"])
    assert successor is not None
    assert successor.parent_job_id == job.job_id
    assert successor.governing_capability_id == "route.cloud.openai"
    assert successor.status == JobStatus.classified.value

    # Three durable events: human.override, job.failed, then job.classified (successor)
    assert len(audit.events) == 3
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[0]["payload"]["override_type"] == "redirect"
    assert audit.events[1]["event_type"] == "job.failed"
    assert audit.events[1]["payload"]["error_class"] == "override_redirect"
    assert audit.events[2]["event_type"] == "job.classified"
    assert audit.events[2]["job_id"] == successor.job_id
    assert audit.events[2]["payload"]["governing_capability_id"] == "route.cloud.openai"


# ---------- e. Redirect delivered job ----------


@pytest.mark.asyncio
async def test_redirect_delivered_job():
    """Redirect a 'delivered' job -> revoked_and_redirected, successor spawned."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.delivered.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="redirect",
        target="routing",
        detail="route.cloud.gemini",
        device="watch",
    )

    assert result["status"] == "revoked_and_redirected"
    assert result["job_id"] == job.job_id
    assert "successor_job_id" in result

    # Original revoked
    assert job.status == JobStatus.revoked.value
    assert job.revoked_at is not None
    assert job.override_type == "redirect"

    # Successor spawned with parent linkage
    successor = jm.get_job(result["successor_job_id"])
    assert successor is not None
    assert successor.parent_job_id == job.job_id
    assert successor.governing_capability_id == "route.cloud.gemini"

    # Events: human.override, job.classified (successor), job.revoked
    assert len(audit.events) == 3
    assert audit.events[0]["event_type"] == "human.override"
    # successor classified event comes before revoke (spawn happens before revoke)
    assert audit.events[1]["event_type"] == "job.classified"
    assert audit.events[1]["job_id"] == successor.job_id
    assert audit.events[2]["event_type"] == "job.revoked"
    assert audit.events[2]["payload"]["reason"] == "escalation_supersede"
    assert audit.events[2]["payload"]["successor_job_id"] == successor.job_id
    assert audit.events[2]["payload"]["override_source_event_id"] == audit.events[0]["source_event_id"]


# ---------- f. Successor job has correct fields ----------


@pytest.mark.asyncio
async def test_successor_job_fields():
    """Successor job carries correct raw_input, parent_job_id, capability, and models."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.dispatched.value,
        raw_input="Compare microservices vs monolith",
        request_category="analysis",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="redirect",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    successor = jm.get_job(result["successor_job_id"])
    assert successor is not None

    # Same raw_input
    assert successor.raw_input == "Compare microservices vs monolith"
    # Parent linkage
    assert successor.parent_job_id == job.job_id
    # Redirect target capability
    assert successor.governing_capability_id == "route.cloud.openai"
    # candidate_models from MODEL_MAP for chatgpt (route.cloud.openai)
    openai_entry = MODEL_MAP["chatgpt"]
    assert successor.candidate_models == [openai_entry["candidate_model"]]
    # Routing recommendation
    assert successor.routing_recommendation == "cloud"
    # Preserves request_category from original
    assert successor.request_category == "analysis"
    # Status is classified (pre-classified)
    assert successor.status == JobStatus.classified.value
    assert successor.classified_at is not None


# ---------- g. Pipeline skips classification for successor ----------


@pytest.mark.asyncio
async def test_pipeline_skips_classification_for_successor():
    """A successor job should NOT call classify() — mock it to raise if called."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    # Create a pre-classified successor job
    successor = _make_job(
        job_id="successor-001",
        status=JobStatus.classified.value,
        governing_capability_id="route.local",
        routing_recommendation="local",
        candidate_models=["llama3.1:8b"],
        request_category="quick_lookup",
        parent_job_id="original-001",
    )
    jm._jobs[successor.job_id] = successor

    # Mock classify to raise — if classification is skipped, test passes
    with patch("job_manager.classify", side_effect=RuntimeError("classify should not be called")):
        # Mock generate_local_response for local dispatch
        mock_local = AsyncMock()
        mock_local.return_value.text = "Paris"
        mock_local.return_value.token_count_in = 10
        mock_local.return_value.token_count_out = 5

        with patch("job_manager.generate_local_response", mock_local):
            await jm._run_pipeline(successor.job_id)

    # Should have completed through the pipeline
    assert successor.status == JobStatus.delivered.value


# ---------- h. Override with invalid override_type ----------


@pytest.mark.asyncio
async def test_override_unknown_type_returns_error():
    """Override with an unknown type returns error via job_manager (not HTTP 400).

    Phase 5D removed the HTTP-level guard — all types pass through to job_manager.
    A truly unknown type gets 'unknown_override_type' from job_manager.
    """
    from fastapi.testclient import TestClient
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)
    job = _make_job(status=JobStatus.dispatched.value)
    jm._jobs[job.job_id] = job

    # Inject our job_manager into the main module
    original_jm = main_module.job_manager
    main_module.job_manager = jm
    try:
        client = TestClient(main_module.app, raise_server_exceptions=False)
        resp = client.post(
            f"/jobs/{job.job_id}/override",
            json={
                "override_type": "bogus",
                "target": "routing",
                "detail": "",
                "device": "phone",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert body["reason"] == "unknown_override_type"
    finally:
        main_module.job_manager = original_jm


# ---------- i. Override on nonexistent job ----------


@pytest.mark.asyncio
async def test_override_nonexistent_job_returns_404():
    """Override on a job_id that doesn't exist returns 404."""
    from fastapi.testclient import TestClient
    import main as main_module

    audit = MockAuditClient()
    jm = _make_manager(audit)

    original_jm = main_module.job_manager
    main_module.job_manager = jm
    try:
        client = TestClient(main_module.app, raise_server_exceptions=False)
        resp = client.post(
            "/jobs/nonexistent-job/override",
            json={
                "override_type": "cancel",
                "target": "routing",
                "detail": "",
                "device": "watch",
            },
        )
        assert resp.status_code == 404
    finally:
        main_module.job_manager = original_jm


# ---------- Additional coverage: cancel at all pre-delivery states ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    JobStatus.submitted.value,
    JobStatus.classified.value,
    JobStatus.dispatched.value,
    JobStatus.response_received.value,
])
async def test_cancel_all_pre_delivery_states(status):
    """Cancel works on every pre-delivery state."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=status)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="phone",
    )

    assert result["status"] == "cancelled"
    assert job.status == JobStatus.failed.value


# ---------- Terminal states: revoked is also terminal ----------


@pytest.mark.asyncio
async def test_cancel_revoked_job_is_noop():
    """A revoked job is terminal — override is no-op."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.revoked.value)
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="watch",
    )

    assert result["status"] == "no_op"
    assert len(audit.events) == 0
