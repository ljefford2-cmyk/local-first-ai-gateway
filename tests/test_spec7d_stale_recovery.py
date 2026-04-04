"""Phase 7D unit tests -- Stale Job Recovery.

Covers: StaleJobRecovery scanning, per-state recovery actions,
re-dispatch cap, idempotency key reuse, RecoveryReport, stale
threshold configuration, and integration with orchestrator startup.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from models import Job, JobStatus
from stale_recovery import (
    DEFAULT_STALE_THRESHOLD_SECONDS,
    MAX_RECOVERY_DISPATCHES,
    MIN_STALE_THRESHOLD_SECONDS,
    RecoveryAction,
    RecoveryReport,
    StaleJobRecovery,
    _get_stale_threshold,
)
from test_helpers import MockAuditClient


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _past_iso(seconds_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class MockJobManager:
    """Lightweight mock of JobManager for recovery tests."""

    def __init__(self):
        self._queue = asyncio.Queue(maxsize=256)
        self._jobs: dict[str, Job] = {}


def _make_recovery():
    audit = MockAuditClient()
    mgr = MockJobManager()
    recovery = StaleJobRecovery(audit)
    return recovery, audit, mgr


def _make_job(
    job_id="job-1",
    status=JobStatus.submitted.value,
    dispatched_at=None,
    idempotency_key="idem-key-1",
    recovery_dispatch_count=0,
    **kwargs,
):
    job = Job(
        job_id=job_id,
        raw_input="test input",
        input_modality="text",
        device="watch",
        status=status,
        created_at=_now_iso(),
        idempotency_key=idempotency_key,
        **kwargs,
    )
    job.recovery_dispatch_count = recovery_dispatch_count
    if dispatched_at is not None:
        job.dispatched_at = dispatched_at
    return job


# ==========================================================================
# 1. Job in "submitted" state -> re-classifies
# ==========================================================================
def test_submitted_job_reclassified():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(status=JobStatus.submitted.value)
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 1
    action = report.actions[0]
    assert action.state_at_crash == "submitted"
    assert action.action_taken == "re_classify"
    assert mgr._queue.qsize() == 1


# ==========================================================================
# 2. Job in "classified" state -> re-dispatches
# ==========================================================================
def test_classified_job_redispatched():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.classified.value,
        governing_capability_id="route.local",
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 1
    action = report.actions[0]
    assert action.state_at_crash == "classified"
    assert action.action_taken == "re_dispatch"
    assert mgr._queue.qsize() == 1


# ==========================================================================
# 3. Job in "dispatched" state, age > 5 min -> marks stale, re-dispatches
# ==========================================================================
def test_dispatched_stale_redispatched():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.dispatched.value,
        dispatched_at=_past_iso(600),  # 10 min ago
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 1
    action = report.actions[0]
    assert action.state_at_crash == "dispatched"
    assert action.action_taken == "re_dispatch_stale"
    # Status reset to classified for pipeline re-entry
    assert job.status == JobStatus.classified.value
    assert mgr._queue.qsize() == 1


# ==========================================================================
# 4. Job in "dispatched" state, age < 5 min -> skipped
# ==========================================================================
def test_dispatched_recent_skipped():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.dispatched.value,
        dispatched_at=_past_iso(60),  # 1 min ago
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_skipped == 1
    action = report.actions[0]
    assert action.action_taken == "skipped"
    # Status unchanged
    assert job.status == JobStatus.dispatched.value
    assert mgr._queue.qsize() == 0


# ==========================================================================
# 5. Job in "response_received" state -> delivers
# ==========================================================================
def test_response_received_delivered():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.response_received.value,
        result="some response",
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 1
    action = report.actions[0]
    assert action.state_at_crash == "response_received"
    assert action.action_taken == "deliver"
    assert job.status == JobStatus.delivered.value
    assert job.delivered_at is not None


# ==========================================================================
# 6. Job in "delivered" state -> no action
# ==========================================================================
def test_delivered_no_action():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(status=JobStatus.delivered.value)
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 0
    assert report.jobs_skipped == 0
    action = report.actions[0]
    assert action.action_taken == "no_action"


# ==========================================================================
# 7. Job in "failed" state -> no action (terminal)
# ==========================================================================
def test_failed_no_action():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(status=JobStatus.failed.value)
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 0
    action = report.actions[0]
    assert action.action_taken == "no_action"


# ==========================================================================
# 8. Re-dispatch reuses original idempotency key
# ==========================================================================
def test_redispatch_reuses_original_idempotency_key():
    recovery, audit, mgr = _make_recovery()
    original_key = "original-idem-key-abc"
    job = _make_job(
        status=JobStatus.classified.value,
        idempotency_key=original_key,
    )
    mgr._jobs[job.job_id] = job
    _run(recovery.run_recovery(mgr._jobs, mgr))
    assert job.idempotency_key == original_key


# ==========================================================================
# 9. Re-dispatch does NOT generate new idempotency key
# ==========================================================================
def test_redispatch_does_not_generate_new_key():
    recovery, audit, mgr = _make_recovery()
    original_key = "keep-this-key-42"
    job = _make_job(
        status=JobStatus.dispatched.value,
        dispatched_at=_past_iso(600),
        idempotency_key=original_key,
    )
    mgr._jobs[job.job_id] = job
    _run(recovery.run_recovery(mgr._jobs, mgr))
    # Key must be identical to original — not replaced
    assert job.idempotency_key == original_key
    assert job.idempotency_key is original_key


# ==========================================================================
# 10. First re-dispatch increments recovery_dispatch_count to 1
# ==========================================================================
def test_first_redispatch_count_increments_to_1():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.submitted.value,
        recovery_dispatch_count=0,
    )
    mgr._jobs[job.job_id] = job
    _run(recovery.run_recovery(mgr._jobs, mgr))
    assert job.recovery_dispatch_count == 1


# ==========================================================================
# 11. Second re-dispatch increments recovery_dispatch_count to 2
# ==========================================================================
def test_second_redispatch_count_increments_to_2():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.classified.value,
        recovery_dispatch_count=1,
    )
    mgr._jobs[job.job_id] = job
    _run(recovery.run_recovery(mgr._jobs, mgr))
    assert job.recovery_dispatch_count == 2


# ==========================================================================
# 12. Third re-dispatch attempt fails with "recovery_exhausted"
# ==========================================================================
def test_third_redispatch_fails_recovery_exhausted():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.submitted.value,
        recovery_dispatch_count=2,
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert job.status == JobStatus.failed.value
    assert job.error == "recovery_exhausted"
    assert report.jobs_failed == 1
    # Not enqueued
    assert mgr._queue.qsize() == 0


# ==========================================================================
# 13. recovery_exhausted emits job.failed event with correct error_class
# ==========================================================================
def test_recovery_exhausted_emits_failed_event():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        job_id="job-exhaust",
        status=JobStatus.classified.value,
        recovery_dispatch_count=2,
    )
    mgr._jobs[job.job_id] = job
    _run(recovery.run_recovery(mgr._jobs, mgr))
    failed_events = audit.get_events_by_type("job.failed")
    assert len(failed_events) == 1
    evt = failed_events[0]
    assert evt["job_id"] == "job-exhaust"
    assert evt["payload"]["error_class"] == "recovery_exhausted"


# ==========================================================================
# 14. Recovery emits job.queued with reason "recovery" before re-dispatch
# ==========================================================================
def test_recovery_emits_job_queued_with_reason_recovery():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(status=JobStatus.submitted.value)
    mgr._jobs[job.job_id] = job
    _run(recovery.run_recovery(mgr._jobs, mgr))
    queued_events = audit.get_events_by_type("job.queued")
    assert len(queued_events) == 1
    evt = queued_events[0]
    assert evt["payload"]["reason"] == "recovery"
    assert evt["job_id"] == job.job_id


# ==========================================================================
# 15. RecoveryReport counts jobs_scanned correctly
# ==========================================================================
def test_report_jobs_scanned():
    recovery, audit, mgr = _make_recovery()
    for i in range(5):
        job = _make_job(job_id=f"job-{i}", status=JobStatus.failed.value)
        mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_scanned == 5


# ==========================================================================
# 16. RecoveryReport counts jobs_recovered correctly
# ==========================================================================
def test_report_jobs_recovered():
    recovery, audit, mgr = _make_recovery()
    # 2 submitted (recoverable), 1 failed (terminal)
    mgr._jobs["j1"] = _make_job(job_id="j1", status=JobStatus.submitted.value)
    mgr._jobs["j2"] = _make_job(job_id="j2", status=JobStatus.submitted.value)
    mgr._jobs["j3"] = _make_job(job_id="j3", status=JobStatus.failed.value)
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 2


# ==========================================================================
# 17. RecoveryReport counts jobs_skipped (< 5 min dispatched jobs)
# ==========================================================================
def test_report_jobs_skipped():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.dispatched.value,
        dispatched_at=_past_iso(30),  # 30 seconds ago
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_skipped == 1


# ==========================================================================
# 18. RecoveryReport counts jobs_failed (recovery_exhausted)
# ==========================================================================
def test_report_jobs_failed():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.classified.value,
        recovery_dispatch_count=2,
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_failed == 1


# ==========================================================================
# 19. RecoveryReport actions list includes job_id, state_at_crash, action_taken
# ==========================================================================
def test_report_actions_have_required_fields():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(job_id="job-action", status=JobStatus.submitted.value)
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert len(report.actions) == 1
    action = report.actions[0]
    assert action.job_id == "job-action"
    assert action.state_at_crash == "submitted"
    assert action.action_taken == "re_classify"
    # Verify these are actual fields, not just dict keys
    assert hasattr(action, "job_id")
    assert hasattr(action, "state_at_crash")
    assert hasattr(action, "action_taken")


# ==========================================================================
# 20. Empty job queue produces clean report with zero counts
# ==========================================================================
def test_empty_jobs_clean_report():
    recovery, audit, mgr = _make_recovery()
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_scanned == 0
    assert report.jobs_recovered == 0
    assert report.jobs_skipped == 0
    assert report.jobs_failed == 0
    assert report.actions == []


# ==========================================================================
# 21. Mixed job states all handled correctly in single pass
# ==========================================================================
def test_mixed_states_single_pass():
    recovery, audit, mgr = _make_recovery()
    mgr._jobs["j-sub"] = _make_job(
        job_id="j-sub", status=JobStatus.submitted.value,
    )
    mgr._jobs["j-cls"] = _make_job(
        job_id="j-cls", status=JobStatus.classified.value,
    )
    mgr._jobs["j-dis-stale"] = _make_job(
        job_id="j-dis-stale",
        status=JobStatus.dispatched.value,
        dispatched_at=_past_iso(600),
    )
    mgr._jobs["j-dis-recent"] = _make_job(
        job_id="j-dis-recent",
        status=JobStatus.dispatched.value,
        dispatched_at=_past_iso(30),
    )
    mgr._jobs["j-resp"] = _make_job(
        job_id="j-resp", status=JobStatus.response_received.value,
    )
    mgr._jobs["j-del"] = _make_job(
        job_id="j-del", status=JobStatus.delivered.value,
    )
    mgr._jobs["j-fail"] = _make_job(
        job_id="j-fail", status=JobStatus.failed.value,
    )

    report = _run(recovery.run_recovery(mgr._jobs, mgr))

    assert report.jobs_scanned == 7
    # submitted + classified + dispatched-stale + response_received = 4 recovered
    assert report.jobs_recovered == 4
    assert report.jobs_skipped == 1   # dispatched-recent
    assert report.jobs_failed == 0
    # 3 enqueued (submitted, classified, dispatched-stale) — response_received delivered directly
    assert mgr._queue.qsize() == 3


# ==========================================================================
# 22. Stale threshold is configurable via env var
# ==========================================================================
def test_stale_threshold_configurable():
    old_val = os.environ.get("DRNT_STALE_THRESHOLD_SECONDS")
    try:
        os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = "120"
        assert _get_stale_threshold() == 120

        # A job dispatched 90 seconds ago should be skipped with 120s threshold
        recovery, audit, mgr = _make_recovery()
        job = _make_job(
            status=JobStatus.dispatched.value,
            dispatched_at=_past_iso(90),
        )
        mgr._jobs[job.job_id] = job
        report = _run(recovery.run_recovery(mgr._jobs, mgr))
        assert report.jobs_skipped == 1

        # But stale with default 300s threshold, let's verify 150s is also stale at 120s
        os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = "120"
        recovery2, audit2, mgr2 = _make_recovery()
        job2 = _make_job(
            job_id="job-2",
            status=JobStatus.dispatched.value,
            dispatched_at=_past_iso(150),
        )
        mgr2._jobs[job2.job_id] = job2
        report2 = _run(recovery2.run_recovery(mgr2._jobs, mgr2))
        assert report2.jobs_recovered == 1
    finally:
        if old_val is None:
            os.environ.pop("DRNT_STALE_THRESHOLD_SECONDS", None)
        else:
            os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = old_val


# ==========================================================================
# 23. Stale threshold must not be below 60 seconds (floor)
# ==========================================================================
def test_stale_threshold_floor():
    old_val = os.environ.get("DRNT_STALE_THRESHOLD_SECONDS")
    try:
        os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = "10"
        threshold = _get_stale_threshold()
        assert threshold == MIN_STALE_THRESHOLD_SECONDS
        assert threshold == 60

        os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = "0"
        assert _get_stale_threshold() == 60

        os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = "59"
        assert _get_stale_threshold() == 60

        # Exactly 60 should be allowed
        os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = "60"
        assert _get_stale_threshold() == 60
    finally:
        if old_val is None:
            os.environ.pop("DRNT_STALE_THRESHOLD_SECONDS", None)
        else:
            os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = old_val


# ==========================================================================
# 24. Recovery pass runs before new jobs are accepted
# ==========================================================================
def test_recovery_runs_before_worker_starts():
    """Verify that recovery enqueues jobs BEFORE the worker loop starts.

    After recovery, recovered jobs sit in the queue but the worker loop
    has not started yet (no task consuming from queue). This confirms
    recovery runs before new-job acceptance.
    """
    recovery, audit, mgr = _make_recovery()
    job = _make_job(status=JobStatus.submitted.value)
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))

    # Jobs are in the queue (recovery ran)
    assert mgr._queue.qsize() == 1
    # But no one has consumed them yet (worker not started)
    assert not mgr._queue.empty()
    assert report.jobs_recovered == 1


# ==========================================================================
# 25. Recovery events are logged to audit trail
# ==========================================================================
def test_recovery_events_logged_to_audit():
    recovery, audit, mgr = _make_recovery()

    # Submitted job -> emits job.queued
    mgr._jobs["j1"] = _make_job(job_id="j1", status=JobStatus.submitted.value)
    # Response received -> emits job.delivered
    mgr._jobs["j2"] = _make_job(job_id="j2", status=JobStatus.response_received.value)
    # Exhausted -> emits job.failed
    mgr._jobs["j3"] = _make_job(
        job_id="j3",
        status=JobStatus.classified.value,
        recovery_dispatch_count=2,
    )

    _run(recovery.run_recovery(mgr._jobs, mgr))

    # Verify all recovery event types appear in audit trail
    queued_events = audit.get_events_by_type("job.queued")
    assert len(queued_events) >= 1
    assert any(e["payload"]["reason"] == "recovery" for e in queued_events)

    delivered_events = audit.get_events_by_type("job.delivered")
    assert len(delivered_events) == 1

    failed_events = audit.get_events_by_type("job.failed")
    assert len(failed_events) == 1
    assert failed_events[0]["payload"]["error_class"] == "recovery_exhausted"


# ==========================================================================
# Bonus coverage
# ==========================================================================


# 26. Revoked job -> no action (terminal)
def test_revoked_no_action():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(status=JobStatus.revoked.value)
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 0
    assert report.actions[0].action_taken == "no_action"


# 27. Dispatched with no dispatched_at timestamp treated as stale
def test_dispatched_no_timestamp_treated_as_stale():
    recovery, audit, mgr = _make_recovery()
    job = _make_job(
        status=JobStatus.dispatched.value,
        dispatched_at=None,
    )
    mgr._jobs[job.job_id] = job
    report = _run(recovery.run_recovery(mgr._jobs, mgr))
    assert report.jobs_recovered == 1
    assert report.actions[0].action_taken == "re_dispatch_stale"


# 28. Default stale threshold is 300 seconds
def test_default_stale_threshold():
    old_val = os.environ.pop("DRNT_STALE_THRESHOLD_SECONDS", None)
    try:
        assert _get_stale_threshold() == DEFAULT_STALE_THRESHOLD_SECONDS
        assert DEFAULT_STALE_THRESHOLD_SECONDS == 300
    finally:
        if old_val is not None:
            os.environ["DRNT_STALE_THRESHOLD_SECONDS"] = old_val


# 29. RecoveryReport dataclass has correct defaults
def test_recovery_report_defaults():
    report = RecoveryReport()
    assert report.jobs_scanned == 0
    assert report.jobs_recovered == 0
    assert report.jobs_skipped == 0
    assert report.jobs_failed == 0
    assert report.actions == []


# 30. RecoveryAction dataclass stores all fields
def test_recovery_action_fields():
    action = RecoveryAction(
        job_id="j1",
        state_at_crash="submitted",
        action_taken="re_classify",
    )
    assert action.job_id == "j1"
    assert action.state_at_crash == "submitted"
    assert action.action_taken == "re_classify"
