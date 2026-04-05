"""Startup recovery pass over non-terminal jobs.

On startup after an unclean shutdown, scans all jobs in non-terminal
states and takes recovery action per Spec 7 Section 4.

Recovery actions by job state at crash:
- submitted (not classified) -> re-classify and route
- classified (not dispatched) -> re-dispatch
- dispatched, no response (>5 min) -> mark stale, re-dispatch with original idempotency key
- dispatched, no response (<5 min) -> wait (skip)
- response_received (not delivered) -> deliver
- delivered (awaiting review) -> no action
- failed/revoked -> no action (terminal)

Re-dispatch cap: max 2 re-dispatches per job. After 2 failures,
job transitions to failed with error_class "recovery_exhausted".

Jobs in non-terminal states are persisted to SQLite and loaded on
startup, enabling recovery after unclean shutdowns.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from events import event_job_delivered, event_job_failed, event_job_queued
from models import Job, JobStatus

logger = logging.getLogger(__name__)

# Default stale threshold: 5 minutes
DEFAULT_STALE_THRESHOLD_SECONDS = 300

# Minimum allowed stale threshold (floor)
MIN_STALE_THRESHOLD_SECONDS = 60

# Maximum re-dispatches per job during recovery
MAX_RECOVERY_DISPATCHES = 2

# States where no recovery action is taken
NO_ACTION_STATES = frozenset({
    JobStatus.failed.value,
    JobStatus.revoked.value,
    JobStatus.delivered.value,
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_stale_threshold() -> int:
    """Return the stale threshold in seconds, respecting env var and floor."""
    raw = os.environ.get("DRNT_STALE_THRESHOLD_SECONDS")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Invalid DRNT_STALE_THRESHOLD_SECONDS=%r, using default %d",
                raw, DEFAULT_STALE_THRESHOLD_SECONDS,
            )
            return DEFAULT_STALE_THRESHOLD_SECONDS
        if value < MIN_STALE_THRESHOLD_SECONDS:
            logger.warning(
                "DRNT_STALE_THRESHOLD_SECONDS=%d below floor %d, clamping",
                value, MIN_STALE_THRESHOLD_SECONDS,
            )
            return MIN_STALE_THRESHOLD_SECONDS
        return value
    return DEFAULT_STALE_THRESHOLD_SECONDS


@dataclass
class RecoveryAction:
    """Single recovery action taken on a job."""

    job_id: str
    state_at_crash: str
    action_taken: str


@dataclass
class RecoveryReport:
    """Summary of a recovery pass."""

    jobs_scanned: int = 0
    jobs_recovered: int = 0
    jobs_skipped: int = 0
    jobs_failed: int = 0
    actions: list[RecoveryAction] = field(default_factory=list)


class StaleJobRecovery:
    """Startup recovery pass over non-terminal jobs."""

    def __init__(self, audit_client) -> None:
        self._audit = audit_client

    async def run_recovery(
        self, jobs: dict[str, Job], job_manager
    ) -> RecoveryReport:
        """Scan all non-terminal jobs and take recovery action.

        Args:
            jobs: The in-memory job store (job_id -> Job).
            job_manager: The JobManager instance (for re-enqueuing).

        Returns:
            RecoveryReport summarizing actions taken.
        """
        report = RecoveryReport()
        stale_threshold = _get_stale_threshold()
        now = datetime.now(timezone.utc)

        for job_id, job in list(jobs.items()):
            report.jobs_scanned += 1
            action = await self._recover_job(job, job_manager, stale_threshold, now)

            if action is not None:
                report.actions.append(action)
                if action.action_taken == "skipped":
                    report.jobs_skipped += 1
                elif action.action_taken == "recovery_exhausted":
                    report.jobs_failed += 1
                elif action.action_taken == "no_action":
                    pass
                else:
                    report.jobs_recovered += 1

        return report

    async def _recover_job(
        self,
        job: Job,
        job_manager,
        stale_threshold: int,
        now: datetime,
    ) -> RecoveryAction | None:
        """Determine and execute recovery action for a single job."""
        state = job.status

        if state in NO_ACTION_STATES:
            return RecoveryAction(
                job_id=job.job_id,
                state_at_crash=state,
                action_taken="no_action",
            )

        if state == JobStatus.submitted.value:
            return await self._recover_submitted(job, job_manager)
        elif state == JobStatus.classified.value:
            return await self._recover_classified(job, job_manager)
        elif state == JobStatus.dispatched.value:
            return await self._recover_dispatched(
                job, job_manager, stale_threshold, now,
            )
        elif state == JobStatus.response_received.value:
            return await self._recover_response_received(job)

        return None

    async def _check_dispatch_cap(self, job: Job) -> bool:
        """Check if re-dispatch is allowed. Returns True if allowed.

        If cap exceeded, transitions job to failed and emits event.
        """
        if job.recovery_dispatch_count >= MAX_RECOVERY_DISPATCHES:
            fail_event = event_job_failed(
                job_id=job.job_id,
                error_class="recovery_exhausted",
                detail=f"Recovery re-dispatch cap ({MAX_RECOVERY_DISPATCHES}) exceeded",
            )
            await self._audit.emit_durable(fail_event)
            job.status = JobStatus.failed.value
            job.error = "recovery_exhausted"
            return False
        return True

    async def _enqueue_for_recovery(self, job: Job, job_manager) -> None:
        """Emit job.queued event and re-enqueue the job for pipeline processing."""
        job.recovery_dispatch_count += 1

        queued_event = event_job_queued(
            job_id=job.job_id,
            reason="recovery",
            position=0,
        )
        await self._audit.emit_durable(queued_event)

        try:
            job_manager._queue.put_nowait(job.job_id)
        except Exception:
            logger.error("Failed to re-enqueue job %s for recovery", job.job_id)

    async def _recover_submitted(self, job: Job, job_manager) -> RecoveryAction:
        """Submitted but not classified -> re-classify and route."""
        if not await self._check_dispatch_cap(job):
            return RecoveryAction(
                job_id=job.job_id,
                state_at_crash=JobStatus.submitted.value,
                action_taken="recovery_exhausted",
            )

        await self._enqueue_for_recovery(job, job_manager)

        return RecoveryAction(
            job_id=job.job_id,
            state_at_crash=JobStatus.submitted.value,
            action_taken="re_classify",
        )

    async def _recover_classified(self, job: Job, job_manager) -> RecoveryAction:
        """Classified but not dispatched -> re-dispatch."""
        if not await self._check_dispatch_cap(job):
            return RecoveryAction(
                job_id=job.job_id,
                state_at_crash=JobStatus.classified.value,
                action_taken="recovery_exhausted",
            )

        await self._enqueue_for_recovery(job, job_manager)

        return RecoveryAction(
            job_id=job.job_id,
            state_at_crash=JobStatus.classified.value,
            action_taken="re_dispatch",
        )

    async def _recover_dispatched(
        self,
        job: Job,
        job_manager,
        stale_threshold: int,
        now: datetime,
    ) -> RecoveryAction:
        """Dispatched but no response -> check staleness."""
        if job.dispatched_at is None:
            age_seconds = stale_threshold + 1
        else:
            dispatched_dt = datetime.fromisoformat(
                job.dispatched_at.replace("Z", "+00:00")
            )
            age_seconds = (now - dispatched_dt).total_seconds()

        if age_seconds < stale_threshold:
            return RecoveryAction(
                job_id=job.job_id,
                state_at_crash=JobStatus.dispatched.value,
                action_taken="skipped",
            )

        # Stale -> re-dispatch with original idempotency key
        if not await self._check_dispatch_cap(job):
            return RecoveryAction(
                job_id=job.job_id,
                state_at_crash=JobStatus.dispatched.value,
                action_taken="recovery_exhausted",
            )

        # Reset to classified so pipeline re-dispatches from that point
        job.status = JobStatus.classified.value

        await self._enqueue_for_recovery(job, job_manager)

        return RecoveryAction(
            job_id=job.job_id,
            state_at_crash=JobStatus.dispatched.value,
            action_taken="re_dispatch_stale",
        )

    async def _recover_response_received(self, job: Job) -> RecoveryAction:
        """Response received but not delivered -> deliver."""
        deliver_event = event_job_delivered(job_id=job.job_id)
        await self._audit.emit_durable(deliver_event)

        job.status = JobStatus.delivered.value
        job.delivered_at = _now_iso()

        return RecoveryAction(
            job_id=job.job_id,
            state_at_crash=JobStatus.response_received.value,
            action_taken="deliver",
        )
