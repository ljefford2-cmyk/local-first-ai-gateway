"""Phase 5E tests — auto-accept window + override conflict guard."""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone, timedelta

import pytest

# Add orchestrator to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from job_manager import JobManager, AUTO_ACCEPT_WINDOW_SECONDS
from models import Job, JobStatus
from test_helpers import MockAuditClient


def _make_job(
    job_id: str = "job-5e-001",
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
    override_type: str | None = None,
    wal_level: int | None = None,
    delivered_at: str | None = None,
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
        override_type=override_type,
        wal_level=wal_level,
        delivered_at=delivered_at,
    )


def _make_manager(audit: MockAuditClient) -> JobManager:
    """Create a JobManager with a mock audit client, no background worker."""
    return JobManager(audit_client=audit)


def _expired_delivered_at() -> str:
    """Return a delivered_at timestamp that is past the auto-accept window."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=AUTO_ACCEPT_WINDOW_SECONDS + 1)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _recent_delivered_at() -> str:
    """Return a delivered_at timestamp within the auto-accept window."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=100)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ==========================================================================
# Override conflict tests (§10-11)
# ==========================================================================


# ---------- a. Second override on modified job is no-op ----------


@pytest.mark.asyncio
async def test_second_override_on_modified_job_is_noop():
    """A delivered job with override_type='modify' rejects a second cancel override."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        status=JobStatus.delivered.value,
        override_type="modify",
        result="Modified result",
        result_id="res-001",
        delivered_at="2026-04-03T00:01:00.000000Z",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="Cancel after modify",
        device="phone",
    )

    assert result["status"] == "no_op"
    assert result["reason"] == "already_overridden"
    assert len(audit.events) == 0


# ---------- b. Second override on cancelled job is no-op ----------


@pytest.mark.asyncio
async def test_second_override_on_cancelled_job_is_noop():
    """A failed job with override_type='cancel' rejects a redirect override."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-002",
        status=JobStatus.failed.value,
        override_type="cancel",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="redirect",
        target="routing",
        detail="route.cloud.openai",
        device="phone",
    )

    assert result["status"] == "no_op"
    # Could be either "job_already_terminal" or "already_overridden" —
    # terminal check fires first for failed jobs
    assert result["reason"] in ("job_already_terminal", "already_overridden")
    assert len(audit.events) == 0


# ---------- c. Second override on redirected job is no-op ----------


@pytest.mark.asyncio
async def test_second_override_on_redirected_job_is_noop():
    """A failed job with override_type='redirect' rejects a cancel override."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-003",
        status=JobStatus.failed.value,
        override_type="redirect",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="Cancel after redirect",
        device="phone",
    )

    assert result["status"] == "no_op"
    assert result["reason"] in ("job_already_terminal", "already_overridden")
    assert len(audit.events) == 0


# ---------- d. Second override on escalated job is no-op ----------


@pytest.mark.asyncio
async def test_second_override_on_escalated_job_is_noop():
    """A revoked job with override_type='escalate' rejects a cancel override."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-004",
        status=JobStatus.revoked.value,
        override_type="escalate",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="Cancel after escalate",
        device="phone",
    )

    assert result["status"] == "no_op"
    assert result["reason"] in ("job_already_terminal", "already_overridden")
    assert len(audit.events) == 0


# ---------- e. Delivered job with no prior override is still overridable ----------


@pytest.mark.asyncio
async def test_delivered_job_no_prior_override_is_overridable():
    """A delivered job with override_type=None can be cancelled (revoked)."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-005",
        status=JobStatus.delivered.value,
        override_type=None,
        result="Delivered result",
        result_id="res-005",
        delivered_at="2026-04-03T00:01:00.000000Z",
    )
    jm._jobs[job.job_id] = job

    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="User cancel",
        device="phone",
    )

    assert result["status"] == "revoked"
    assert job.status == JobStatus.revoked.value
    assert job.override_type == "cancel"


# ==========================================================================
# Auto-accept tests (§9)
# ==========================================================================


# ---------- f. Auto-accept fires for WAL-2+ delivered job after window ----------


@pytest.mark.asyncio
async def test_auto_accept_fires_for_wal2_after_window():
    """WAL-2 delivered job past the window gets auto-accepted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-001",
        status=JobStatus.delivered.value,
        wal_level=2,
        delivered_at=_expired_delivered_at(),
    )
    jm._jobs[job.job_id] = job

    await jm._check_auto_accept(job)

    # Verify human.reviewed event
    reviewed = audit.get_events_by_type("human.reviewed")
    assert len(reviewed) == 1
    assert reviewed[0]["payload"]["decision"] == "auto_delivered"
    assert reviewed[0]["payload"]["device"] == "system"
    assert reviewed[0]["payload"]["review_latency_ms"] == AUTO_ACCEPT_WINDOW_SECONDS * 1000

    # Verify job locked
    assert job.override_type == "auto_delivered"


# ---------- g. Auto-accept does NOT fire for WAL-0 job ----------


@pytest.mark.asyncio
async def test_auto_accept_does_not_fire_for_wal0():
    """WAL-0 delivered job is not auto-accepted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-002",
        status=JobStatus.delivered.value,
        wal_level=0,
        delivered_at=_expired_delivered_at(),
    )
    jm._jobs[job.job_id] = job

    await jm._check_auto_accept(job)

    assert len(audit.events) == 0
    assert job.override_type is None


# ---------- h. Auto-accept does NOT fire for WAL-1 job ----------


@pytest.mark.asyncio
async def test_auto_accept_does_not_fire_for_wal1():
    """WAL-1 delivered job is not auto-accepted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-003",
        status=JobStatus.delivered.value,
        wal_level=1,
        delivered_at=_expired_delivered_at(),
    )
    jm._jobs[job.job_id] = job

    await jm._check_auto_accept(job)

    assert len(audit.events) == 0
    assert job.override_type is None


# ---------- i. Auto-accept does NOT fire before window expires ----------


@pytest.mark.asyncio
async def test_auto_accept_does_not_fire_before_window():
    """WAL-2 delivered job within the window is not auto-accepted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-004",
        status=JobStatus.delivered.value,
        wal_level=2,
        delivered_at=_recent_delivered_at(),
    )
    jm._jobs[job.job_id] = job

    await jm._check_auto_accept(job)

    assert len(audit.events) == 0
    assert job.override_type is None


# ---------- j. Auto-accept does NOT fire for already-overridden job ----------


@pytest.mark.asyncio
async def test_auto_accept_does_not_fire_for_already_overridden():
    """WAL-2 delivered job with override_type='modify' is not auto-accepted."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-005",
        status=JobStatus.delivered.value,
        wal_level=2,
        override_type="modify",
        delivered_at=_expired_delivered_at(),
    )
    jm._jobs[job.job_id] = job

    await jm._check_auto_accept(job)

    assert len(audit.events) == 0
    assert job.override_type == "modify"  # unchanged


# ---------- k. Auto-accept does NOT fire for non-delivered job ----------


@pytest.mark.asyncio
async def test_auto_accept_does_not_fire_for_non_delivered():
    """WAL-2 dispatched job is not auto-accepted (wrong status)."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-006",
        status=JobStatus.dispatched.value,
        wal_level=2,
    )
    jm._jobs[job.job_id] = job

    await jm._check_auto_accept(job)

    assert len(audit.events) == 0
    assert job.override_type is None


# ---------- l. Auto-accepted job rejects further overrides ----------


@pytest.mark.asyncio
async def test_auto_accepted_job_rejects_further_overrides():
    """After auto-accept (override_type='auto_delivered'), cancel returns no_op."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    job = _make_job(
        job_id="job-5e-aa-007",
        status=JobStatus.delivered.value,
        wal_level=2,
        delivered_at=_expired_delivered_at(),
        result="Some result",
        result_id="res-aa-007",
    )
    jm._jobs[job.job_id] = job

    # Auto-accept fires
    await jm._check_auto_accept(job)
    assert job.override_type == "auto_delivered"

    audit.clear()

    # Attempt cancel — should be no-op
    result = await jm.override_job(
        job_id=job.job_id,
        override_type="cancel",
        target="routing",
        detail="Cancel after auto-accept",
        device="phone",
    )

    assert result["status"] == "no_op"
    assert result["reason"] == "already_overridden"
    assert len(audit.events) == 0


# ---------- m. Auto-accept loop processes multiple jobs ----------


@pytest.mark.asyncio
async def test_auto_accept_loop_processes_multiple_jobs():
    """Three jobs: WAL-2 expired (accepts), WAL-0 expired (skips), WAL-2 recent (skips)."""
    audit = MockAuditClient()
    jm = _make_manager(audit)

    # Job 1: WAL-2, expired — should auto-accept
    job1 = _make_job(
        job_id="job-5e-multi-001",
        status=JobStatus.delivered.value,
        wal_level=2,
        delivered_at=_expired_delivered_at(),
    )

    # Job 2: WAL-0, expired — should NOT auto-accept
    job2 = _make_job(
        job_id="job-5e-multi-002",
        status=JobStatus.delivered.value,
        wal_level=0,
        delivered_at=_expired_delivered_at(),
    )

    # Job 3: WAL-2, recent — should NOT auto-accept
    job3 = _make_job(
        job_id="job-5e-multi-003",
        status=JobStatus.delivered.value,
        wal_level=2,
        delivered_at=_recent_delivered_at(),
    )

    jm._jobs[job1.job_id] = job1
    jm._jobs[job2.job_id] = job2
    jm._jobs[job3.job_id] = job3

    # Call _check_auto_accept on each (simulating what the loop does)
    for job in [job1, job2, job3]:
        await jm._check_auto_accept(job)

    # Only job1 should have been auto-accepted
    reviewed = audit.get_events_by_type("human.reviewed")
    assert len(reviewed) == 1
    assert reviewed[0]["job_id"] == "job-5e-multi-001"
    assert reviewed[0]["payload"]["decision"] == "auto_delivered"

    assert job1.override_type == "auto_delivered"
    assert job2.override_type is None
    assert job3.override_type is None
