"""Phase 5C tests — conditional demotion integration for cancel/redirect overrides."""

from __future__ import annotations

import sys
import os

import pytest

# Add orchestrator to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from job_manager import JobManager
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
        last_failing_capability_id=last_failing_capability_id,
    )


def _make_manager(audit: MockAuditClient) -> JobManager:
    """Create a JobManager with demotion enabled."""
    return JobManager(audit_client=audit, demotion_engine=_DEMOTION_ENABLED)


# ---------- a. Cancel on active job triggers demotion ----------


@pytest.mark.asyncio
async def test_cancel_active_job_triggers_demotion():
    """Cancel a job in 'dispatched' state with no prior sentinel failure -> demotion fires."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.dispatched.value)
    jm._jobs[job.job_id] = job

    await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="watch",
    )

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 1

    evt = demoted_events[0]
    assert evt["payload"]["trigger"] == "override"
    assert evt["payload"]["capability_id"] == "route.cloud.claude"

    # wal.demoted comes after human.override and job.failed
    types = [e["event_type"] for e in audit.events]
    assert "human.override" in types
    assert "job.failed" in types
    assert "wal.demoted" in types


# ---------- b. Cancel with prior sentinel failure suppresses demotion ----------


@pytest.mark.asyncio
async def test_cancel_with_sentinel_suppresses_demotion():
    """Cancel with prior sentinel failure -> NO wal.demoted event."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.dispatched.value,
        last_failing_capability_id="egress_connectivity",
    )
    jm._jobs[job.job_id] = job

    await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="watch",
    )

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 0

    # Override and failure events still emitted
    assert audit.events[0]["event_type"] == "human.override"
    assert audit.events[1]["event_type"] == "job.failed"


# ---------- c. Redirect on active job triggers demotion ----------


@pytest.mark.asyncio
async def test_redirect_active_job_triggers_demotion():
    """Redirect a job in 'response_received' state -> demotion fires for original capability."""
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

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 1

    evt = demoted_events[0]
    assert evt["payload"]["capability_id"] == "route.cloud.claude"
    assert evt["payload"]["trigger"] == "override"
    assert evt["payload"]["override_type"] == "redirect"


# ---------- d. Redirect with prior sentinel failure suppresses demotion ----------


@pytest.mark.asyncio
async def test_redirect_with_sentinel_suppresses_demotion():
    """Redirect with prior sentinel failure -> NO wal.demoted event."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.response_received.value,
        last_failing_capability_id="egress_config",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="redirect",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    assert result["status"] == "redirected"

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 0


# ---------- e. Cancel on delivered job (revocation) triggers demotion ----------


@pytest.mark.asyncio
async def test_cancel_delivered_job_triggers_demotion():
    """Cancel a delivered job (revoke path) with no sentinel -> demotion fires."""
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

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 1

    evt = demoted_events[0]
    assert evt["payload"]["trigger"] == "override"
    assert evt["payload"]["capability_id"] == "route.cloud.claude"


# ---------- f. Cancel on delivered job with sentinel suppresses demotion ----------


@pytest.mark.asyncio
async def test_cancel_delivered_with_sentinel_suppresses_demotion():
    """Cancel a delivered job with sentinel -> revoke still happens, no demotion."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.delivered.value,
        last_failing_capability_id="worker_sandbox",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="phone",
    )

    assert result["status"] == "revoked"
    assert job.status == JobStatus.revoked.value

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 0


# ---------- g. Demotion event has correct payload ----------


@pytest.mark.asyncio
async def test_demotion_event_payload():
    """Verify wal.demoted event has correct capability_id, levels, trigger, and override_type."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.dispatched.value,
        governing_capability_id="route.cloud.gemini",
    )
    jm._jobs[job.job_id] = job

    await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="watch",
    )

    demoted_events = audit.get_events_by_type("wal.demoted")
    assert len(demoted_events) == 1

    evt = demoted_events[0]
    payload = evt["payload"]
    assert payload["capability_id"] == "route.cloud.gemini"
    assert payload["from_level"] == 0  # WAL-0
    assert payload["to_level"] == -1   # WAL-0 demote by 1 = suspended
    assert payload["trigger"] == "override"
    assert payload["override_type"] == "cancel"

    # Event envelope fields
    assert evt["event_type"] == "wal.demoted"
    assert evt["capability_id"] == "route.cloud.gemini"
    assert evt["wal_level"] == -1
    assert evt["job_id"] == job.job_id


# ---------- h. Event ordering ----------


@pytest.mark.asyncio
async def test_event_ordering_cancel_with_demotion():
    """For cancel with demotion: human.override -> job.failed -> wal.demoted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(status=JobStatus.dispatched.value)
    jm._jobs[job.job_id] = job

    await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="",
        device="phone",
    )

    types = [e["event_type"] for e in audit.events]
    assert types == ["human.override", "job.failed", "wal.demoted"]
