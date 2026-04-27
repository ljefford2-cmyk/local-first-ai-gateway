"""Job lifecycle manager for the DRNT orchestrator.

Manages in-memory job state and drives the async pipeline:
submitted → classified → dispatched → response_received → delivered
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

import httpx
import uuid_utils

from audit_client import AuditLogClient
from idempotency_store import IdempotencyStore
from classifier import classify, generate_local_response
from context_packager import ContextPackager
from classifier import MODEL_MAP
from events import (
    build_event,
    event_egress_fallback_to_local,
    event_human_override,
    event_human_reviewed,
    event_job_classified,
    event_job_closed_no_action,
    event_job_delivered,
    event_job_dispatched,
    event_job_failed,
    event_job_proposal_ready,
    event_job_queued,
    event_job_response_received,
    event_job_revoked,
    event_job_submitted,
    event_model_response,
    event_wal_permission_check,
)
from override_types import CONDITIONAL_DEMOTION_TYPES, TERMINAL_STATES, is_sentinel_failure
from models import Job, JobListResponse, JobStatus, JobStatusResponse, Proposal, ReviewDecision

# Phase 4A.2.d: terminal/resolving review decisions that close the proposal.
# defer is non-terminal and does not close the proposal.
_TERMINAL_REVIEW_DECISIONS = frozenset({
    ReviewDecision.approve.value,
    ReviewDecision.edit.value,
    ReviewDecision.reject.value,
    ReviewDecision.decline_to_act.value,
})
from permission_checker import JobContext, PermissionChecker
from registry import EgressRegistry
from worker_context import WorkerContext

logger = logging.getLogger(__name__)

# Bounded queue size — when full, new submissions get 503
EVENT_QUEUE_BOUND = 256

EGRESS_GATEWAY_URL = os.environ.get("EGRESS_GATEWAY_URL", "http://egress-gateway:8080")
RESULTS_DIR = os.environ.get("DRNT_RESULTS_DIR", "/var/drnt/results")

# Phase 5E: auto-accept window for WAL-2+ delivered jobs (seconds)
AUTO_ACCEPT_WINDOW_SECONDS = int(os.environ.get("DRNT_AUTO_ACCEPT_WINDOW", "86400"))  # 24 hours default
AUTO_ACCEPT_POLL_INTERVAL = int(os.environ.get("DRNT_AUTO_ACCEPT_POLL_INTERVAL", "60"))  # 60 seconds default


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uuid7() -> str:
    return str(uuid_utils.uuid7())


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _write_result(result_id: str, content: str) -> None:
    """Write a result artifact to the results store on disk."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{result_id}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class JobManager:
    """Owns job state and drives the async pipeline."""

    def __init__(
        self,
        audit_client: AuditLogClient,
        context_packager: Optional[ContextPackager] = None,
        egress_registry: Optional[EgressRegistry] = None,
        permission_checker: Optional[PermissionChecker] = None,
        demotion_engine: Optional[object] = None,
        worker_lifecycle: Optional[object] = None,
        connectivity_monitor: Optional[object] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self._audit = audit_client
        self._packager = context_packager
        self._egress_registry = egress_registry
        self._permission_checker = permission_checker
        self._demotion_engine = demotion_engine
        self._worker_lifecycle = worker_lifecycle
        self._connectivity_monitor = connectivity_monitor
        self._jobs: dict[str, Job] = {}
        self._worker_contexts: dict[str, WorkerContext] = {}  # job_id → WorkerContext
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=EVENT_QUEUE_BOUND)
        self._worker_task: Optional[asyncio.Task] = None
        self._auto_accept_task: Optional[asyncio.Task] = None
        # Resolve db_path for SQLite persistence
        if db_path is None:
            try:
                from persistence import get_db_path
                db_path = get_db_path()
            except ImportError:
                pass

        self._idempotency_store = IdempotencyStore(db_path=db_path)

        self._job_db: sqlite3.Connection | None = None
        if db_path is not None:
            try:
                self._job_db = sqlite3.connect(db_path, check_same_thread=False)
                self._job_db.execute("SELECT 1 FROM jobs LIMIT 1")
                self._load_jobs_from_db()
                logger.info("Job store using SQLite at %s", db_path)
            except Exception:
                logger.warning(
                    "SQLite unavailable — falling back to in-memory job store",
                    exc_info=True,
                )
                self._job_db = None

        self._hub_state_manager: Optional[object] = None

    def set_hub_state_manager(self, manager: object) -> None:
        """Attach a HubStateManager for processing gate checks."""
        self._hub_state_manager = manager

    # -- persistence helpers --

    def _load_jobs_from_db(self) -> None:
        """Load non-terminal jobs from SQLite into the in-memory cache."""
        if self._job_db is None:
            return
        try:
            cursor = self._job_db.execute(
                "SELECT job_id, data FROM jobs WHERE status NOT IN (?, ?, ?)",
                (JobStatus.delivered.value, JobStatus.failed.value, JobStatus.revoked.value),
            )
            count = 0
            for row in cursor:
                data = json.loads(row[1])
                self._jobs[row[0]] = Job(**data)
                count += 1
            if count:
                logger.info("Loaded %d non-terminal jobs from database", count)
        except Exception:
            logger.warning("Failed to load jobs from database", exc_info=True)

    def _persist_job(self, job: Job) -> None:
        """Write-through a single job record to SQLite."""
        if self._job_db is None:
            return
        try:
            data = json.dumps(asdict(job))
            self._job_db.execute(
                "INSERT OR REPLACE INTO jobs (job_id, data, status, created_at, idempotency_key) VALUES (?, ?, ?, ?, ?)",
                (job.job_id, data, job.status, job.created_at, job.idempotency_key),
            )
            self._job_db.commit()
        except Exception:
            logger.warning("DB write failed for job %s", job.job_id, exc_info=True)

    # -- lifecycle --

    async def start(self) -> None:
        """Start the background pipeline worker and auto-accept loop."""
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._auto_accept_task = asyncio.create_task(self._auto_accept_loop())

    async def stop(self) -> None:
        """Gracefully stop the background worker and auto-accept loop."""
        if self._auto_accept_task:
            self._auto_accept_task.cancel()
            try:
                await self._auto_accept_task
            except asyncio.CancelledError:
                pass
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    # -- public API --

    async def submit_job(
        self,
        raw_input: str,
        input_modality: str,
        device: str,
        idempotency_key: Optional[str] = None,
    ) -> Job:
        """Create a job, emit job.submitted (durable, blocks until ACK),
        enqueue for async processing, and return the job record.

        If *idempotency_key* is provided and already known, return the
        existing job without creating a duplicate.  If None, a key is
        auto-generated internally (backward compatibility).
        """
        if idempotency_key is None:
            idempotency_key = _uuid7()

        job_id = _uuid7()

        # Idempotency check — if key already exists, return the stored job
        is_new, existing_job_id = self._idempotency_store.check_and_store(
            idempotency_key, job_id,
        )
        if not is_new:
            existing_job = self._jobs.get(existing_job_id)
            if existing_job is not None:
                return existing_job
            # Stored job_id no longer in memory (shouldn't happen in V1)
            # — fall through and create a new job

        now = _now_iso()

        job = Job(
            job_id=job_id,
            raw_input=raw_input,
            input_modality=input_modality,
            device=device,
            status=JobStatus.submitted.value,
            created_at=now,
            idempotency_key=idempotency_key,
        )

        # Emit durable event — blocks until ACK (fail-closed)
        event = event_job_submitted(
            job_id=job_id,
            raw_input=raw_input,
            input_modality=input_modality,
            device=device,
        )
        await self._audit.emit_durable(event)

        self._jobs[job_id] = job
        self._persist_job(job)

        # Enqueue for async pipeline processing
        try:
            self._queue.put_nowait(job_id)
        except asyncio.QueueFull:
            job.status = JobStatus.failed.value
            job.error = "pipeline_queue_full"
            self._persist_job(job)
            raise

        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def get_worker_context(self, job_id: str) -> Optional[WorkerContext]:
        """Return the active WorkerContext for a job, if any."""
        return self._worker_contexts.get(job_id)

    async def override_job(
        self,
        job_id: str,
        override_type: str,
        target: str,
        detail: str,
        device: str,
        modified_result: str | None = None,
    ) -> dict:
        """Execute a cancel, redirect, modify, or escalate override on a job."""
        job = self._jobs.get(job_id)
        if job is None:
            return {"status": "not_found", "job_id": job_id}

        # Terminal jobs (failed, revoked): no-op. Delivered is handled below
        # (it can be revoked via cancel/redirect).
        if job.status in TERMINAL_STATES and job.status != JobStatus.delivered.value:
            return {"status": "no_op", "reason": "job_already_terminal", "job_id": job_id}

        # Phase 4A.2.d: review/override serialization. If a terminal review
        # decision has resolved the job, the override is a no-op (review
        # wins first per Phase 4A.2.d rule 4).
        if job.review_decision in _TERMINAL_REVIEW_DECISIONS:
            return {"status": "no_op", "reason": "already_reviewed", "job_id": job_id}

        # Phase 5E: already-overridden guard — first override wins, second is no-op
        if job.override_type is not None:
            return {"status": "no_op", "reason": "already_overridden", "job_id": job_id}

        # Phase 4A.2.d: proposal_ready first-writer guard. Set override_type
        # before any durable human.override or terminal lifecycle event await
        # so a concurrent review against the same proposal cannot pass its
        # proposal_ready precondition once override has begun. The per-branch
        # handlers below set the same value again at end-of-flow, which is a
        # no-op rewrite. Limited to proposal_ready per plan scope.
        if job.status == JobStatus.proposal_ready.value:
            job.override_type = override_type
            self._persist_job(job)

        pre_delivery = job.status in (
            JobStatus.submitted.value,
            JobStatus.classified.value,
            JobStatus.dispatched.value,
            JobStatus.response_received.value,
        )

        result = None

        if override_type == "cancel":
            if pre_delivery:
                # Cancel pre-delivery
                override_evt = event_human_override(job_id, "cancel", target, detail, device)
                await self._audit.emit_durable(override_evt)

                fail_evt = event_job_failed(
                    job_id=job_id,
                    error_class="override_cancel",
                    detail="Cancelled by user",
                    failing_capability_id=job.governing_capability_id,
                )
                await self._audit.emit_durable(fail_evt)

                job.status = JobStatus.failed.value
                job.error = "override_cancel"
                job.override_type = "cancel"
                result = {"status": "cancelled", "job_id": job_id}
            else:
                # Cancel delivered
                override_evt = event_human_override(job_id, "cancel", target, detail, device)
                await self._audit.emit_durable(override_evt)
                source_event_id = override_evt["source_event_id"]

                revoke_evt = event_job_revoked(
                    job_id=job_id,
                    reason="user_cancel",
                    override_source_event_id=source_event_id,
                    successor_job_id=None,
                    memory_objects_superseded=[],
                )
                await self._audit.emit_durable(revoke_evt)

                job.status = JobStatus.revoked.value
                job.revoked_at = _now_iso()
                job.override_type = "cancel"
                result = {"status": "revoked", "job_id": job_id}

        elif override_type == "redirect":
            if pre_delivery:
                # Redirect pre-delivery
                override_evt = event_human_override(job_id, "redirect", target, detail, device)
                await self._audit.emit_durable(override_evt)

                fail_evt = event_job_failed(
                    job_id=job_id,
                    error_class="override_redirect",
                    detail=f"Redirected to {detail}",
                    failing_capability_id=job.governing_capability_id,
                )
                await self._audit.emit_durable(fail_evt)

                job.status = JobStatus.failed.value
                job.error = "override_redirect"
                job.override_type = "redirect"

                successor = await self._spawn_successor(job, detail)
                result = {"status": "redirected", "job_id": job_id, "successor_job_id": successor.job_id}
            else:
                # Redirect delivered
                override_evt = event_human_override(job_id, "redirect", target, detail, device)
                await self._audit.emit_durable(override_evt)
                source_event_id = override_evt["source_event_id"]

                successor = await self._spawn_successor(job, detail)

                revoke_evt = event_job_revoked(
                    job_id=job_id,
                    reason="escalation_supersede",
                    override_source_event_id=source_event_id,
                    successor_job_id=successor.job_id,
                    memory_objects_superseded=[],
                )
                await self._audit.emit_durable(revoke_evt)

                job.status = JobStatus.revoked.value
                job.revoked_at = _now_iso()
                job.override_type = "redirect"
                result = {"status": "revoked_and_redirected", "job_id": job_id, "successor_job_id": successor.job_id}

        elif override_type == "modify":
            # Validate modified_result is provided
            if not modified_result:
                return {"status": "error", "reason": "modified_result_required", "job_id": job_id}

            has_result = job.status in (
                JobStatus.response_received.value,
                JobStatus.delivered.value,
            )
            if not has_result:
                return {"status": "error", "reason": "no_result_to_modify", "job_id": job_id}

            already_delivered = job.status == JobStatus.delivered.value

            # Emit human.override — durable
            override_evt = event_human_override(job_id, "modify", target, detail, device)
            await self._audit.emit_durable(override_evt)

            # Generate modified result artifact
            modified_result_id = _uuid7()
            modified_result_hash = _sha256(modified_result)
            _write_result(modified_result_id, modified_result)

            # Emit human.reviewed with lineage — durable
            reviewed_evt = event_human_reviewed(
                job_id=job_id,
                decision="modified",
                device=device,
                review_latency_ms=0,
                modification_summary=detail,
                modified_result_id=modified_result_id,
                modified_result_hash=modified_result_hash,
                derived_from_result_id=job.result_id,
            )
            await self._audit.emit_durable(reviewed_evt)

            # Update job result to modified text
            job.result = modified_result
            job.override_type = "modify"

            if not already_delivered:
                # Pre-delivery modify implies acceptance — deliver
                deliver_evt = event_job_delivered(job_id=job_id)
                await self._audit.emit_durable(deliver_evt)
                job.status = JobStatus.delivered.value
                job.delivered_at = _now_iso()

            result = {"status": "modified", "job_id": job_id, "modified_result_id": modified_result_id}

        elif override_type == "escalate":
            # Emit human.override — durable
            override_evt = event_human_override(job_id, "escalate", target, detail, device)
            await self._audit.emit_durable(override_evt)
            source_event_id = override_evt["source_event_id"]

            # Spawn successor — detail is the target capability_id
            successor = await self._spawn_successor(job, detail)

            # Revoke original
            revoke_evt = event_job_revoked(
                job_id=job_id,
                reason="escalation_supersede",
                override_source_event_id=source_event_id,
                successor_job_id=successor.job_id,
                memory_objects_superseded=[],
            )
            await self._audit.emit_durable(revoke_evt)

            job.status = JobStatus.revoked.value
            job.revoked_at = _now_iso()
            job.override_type = "escalate"
            result = {"status": "escalated", "job_id": job_id, "successor_job_id": successor.job_id}

        if result is not None:
            self._persist_job(job)
            # Phase 6E: teardown original worker context on override
            original_wctx = self._worker_contexts.pop(job_id, None)
            if original_wctx is not None and self._worker_lifecycle is not None:
                original_wctx.status = "failed"
                await self._worker_lifecycle.teardown_worker(original_wctx)

            # Phase 5C: conditional demotion for cancel/redirect overrides
            if override_type in CONDITIONAL_DEMOTION_TYPES:
                if not is_sentinel_failure(job.last_failing_capability_id):
                    await self._demote_for_override(job.governing_capability_id, job.job_id, override_type)
                else:
                    logger.info("Demotion suppressed for job %s — prior sentinel failure: %s",
                                job.job_id, job.last_failing_capability_id)
            return result

        return {"status": "error", "reason": "unknown_override_type", "job_id": job_id}

    # -- Phase 4A.2.d: review handler --

    async def review_job(
        self,
        job_id: str,
        decision: str,
        result_id: str,
        response_hash: str,
        decision_idempotency_key: str,
        modified_result: str | None = None,
        device: str = "phone",
    ) -> tuple[int, dict]:
        """Handle POST /jobs/{job_id}/review per Phase 4A.2.c/d semantics.

        Returns (status_code, body) so the HTTP route can faithfully replay
        the same envelope on idempotency-key replay without re-running any
        durable emit or state mutation.
        """
        # Build the payload identity used for idempotency comparison.
        # Per Phase 4A.2.c rule 4 the identity includes job_id, decision,
        # result_id, response_hash, modified_result, and the key itself.
        payload_identity = {
            "job_id": job_id,
            "decision": decision,
            "result_id": result_id,
            "response_hash": response_hash,
            "modified_result": modified_result,
            "decision_idempotency_key": decision_idempotency_key,
        }

        # Phase 4A.2.c rule 2: idempotency replay BEFORE stale-decision checks.
        existing = self._idempotency_store.get_review_outcome(
            decision_idempotency_key,
        )
        if existing is not None:
            if existing.payload_identity == payload_identity:
                # Same key + same payload: replay original outcome without
                # repeating state transitions or emitting duplicate events.
                return (
                    int(existing.outcome["status_code"]),
                    existing.outcome["body"],
                )
            # Same key + different payload: 409, no mutation, no event.
            return (
                409,
                {
                    "error": "decision_idempotency_key_conflict",
                    "message": (
                        "decision_idempotency_key already used with a "
                        "different review payload."
                    ),
                },
            )

        # Job lookup. An unknown job_id is a 404 — distinct from the
        # wrong-status 409 path because there is no current_status to
        # report.
        job = self._jobs.get(job_id)
        if job is None:
            return (404, {"error": "job_not_found", "job_id": job_id})

        # Phase 4A.2.c rule 1: review valid only when status == proposal_ready.
        if job.status != JobStatus.proposal_ready.value:
            return self._review_wrong_status_response(job)

        # Phase 4A.2.d rule 4: review must reject when override has won first.
        if job.override_type is not None:
            return self._review_wrong_status_response(job)

        # Phase 4A.2.c rule 2: stale-decision checks BEFORE any state
        # mutation or durable review event when the key is unknown.
        if (
            job.result_id != result_id
            or job.response_hash != response_hash
        ):
            return self._review_stale_response(job)

        # Phase 4A.2.c rule 6: handler-side modified_result rule. 422 on
        # violation. The schema admits modified_result as Optional[str]; the
        # required-iff-edit constraint is enforced here.
        if decision == ReviewDecision.edit.value:
            if not isinstance(modified_result, str) or modified_result == "":
                return (
                    422,
                    {
                        "error": "modified_result_required",
                        "message": (
                            "modified_result is required when decision == 'edit'"
                        ),
                    },
                )
        else:
            if modified_result is not None:
                return (
                    422,
                    {
                        "error": "modified_result_not_allowed",
                        "message": (
                            "modified_result must be absent or null when "
                            f"decision == '{decision}'"
                        ),
                    },
                )

        # Per-decision branches.
        if decision == ReviewDecision.approve.value:
            return await self._review_approve(
                job, payload_identity, decision_idempotency_key, device,
            )
        if decision == ReviewDecision.edit.value:
            return await self._review_edit(
                job,
                payload_identity,
                decision_idempotency_key,
                modified_result,
                device,
            )
        if decision == ReviewDecision.reject.value:
            return await self._review_reject(
                job, payload_identity, decision_idempotency_key, device,
            )
        if decision == ReviewDecision.defer.value:
            return await self._review_defer(
                job, payload_identity, decision_idempotency_key, device,
            )
        if decision == ReviewDecision.decline_to_act.value:
            return await self._review_decline_to_act(
                job, payload_identity, decision_idempotency_key, device,
            )

        # Unreachable when the route validates ReviewRequest.decision against
        # the ReviewDecision enum, but kept for defensive completeness.
        return (
            422,
            {"error": "unknown_decision", "decision": decision},
        )

    def _review_wrong_status_response(self, job: Job) -> tuple[int, dict]:
        """Locked Phase 4A.2.c rule 1 wrong-status / current-status 409 body."""
        return (
            409,
            {
                "error": "wrong_status",
                "current_status": job.status,
                "current_result_id": job.result_id,
                "current_response_hash": job.response_hash,
                "message": (
                    f"Review not permitted for job in status '{job.status}'; "
                    "only proposal_ready jobs without an override are reviewable."
                ),
            },
        )

    def _review_stale_response(self, job: Job) -> tuple[int, dict]:
        """Locked Phase 4A.2.c rule 2 stale-decision 409 body."""
        return (
            409,
            {
                "error": "stale_decision",
                "current_status": job.status,
                "current_result_id": job.result_id,
                "current_response_hash": job.response_hash,
                "message": (
                    "Review request result_id/response_hash does not match "
                    "the current authoritative proposal."
                ),
            },
        )

    async def _review_approve(
        self,
        job: Job,
        payload_identity: dict,
        decision_idempotency_key: str,
        device: str,
    ) -> tuple[int, dict]:
        # Phase 4A.2.d rule 3: set first-writer guard before any durable emit.
        job.review_decision = ReviewDecision.approve.value
        self._persist_job(job)

        reviewed_evt = event_human_reviewed(
            job_id=job.job_id,
            decision=ReviewDecision.approve.value,
            device=device,
            review_latency_ms=0,
        )
        await self._audit.emit_durable(reviewed_evt)

        deliver_evt = event_job_delivered(job_id=job.job_id)
        await self._audit.emit_durable(deliver_evt)

        job.status = JobStatus.delivered.value
        job.delivered_at = _now_iso()
        self._persist_job(job)

        body = {
            "status": "delivered",
            "job_id": job.job_id,
            "decision": ReviewDecision.approve.value,
            "result_id": job.result_id,
            "response_hash": job.response_hash,
        }
        self._idempotency_store.store_review_outcome(
            decision_idempotency_key,
            payload_identity,
            {"status_code": 200, "body": body},
            applied=True,
        )
        return (200, body)

    async def _review_edit(
        self,
        job: Job,
        payload_identity: dict,
        decision_idempotency_key: str,
        modified_result: str,
        device: str,
    ) -> tuple[int, dict]:
        # Phase 4A.2.d rule 3: set first-writer guard before any durable emit.
        job.review_decision = ReviewDecision.edit.value
        self._persist_job(job)

        modified_result_id = _uuid7()
        modified_result_hash = _sha256(modified_result)
        _write_result(modified_result_id, modified_result)
        derived_from_result_id = job.result_id

        reviewed_evt = event_human_reviewed(
            job_id=job.job_id,
            decision=ReviewDecision.edit.value,
            device=device,
            review_latency_ms=0,
            modified_result_id=modified_result_id,
            modified_result_hash=modified_result_hash,
            derived_from_result_id=derived_from_result_id,
        )
        await self._audit.emit_durable(reviewed_evt)

        deliver_evt = event_job_delivered(job_id=job.job_id)
        await self._audit.emit_durable(deliver_evt)

        # Phase 4A.2.c rule 5: edit updates authoritative result identity to
        # the modified artifact and transitions the job to delivered.
        job.result = modified_result
        job.result_id = modified_result_id
        job.response_hash = modified_result_hash
        job.status = JobStatus.delivered.value
        job.delivered_at = _now_iso()
        self._persist_job(job)

        body = {
            "status": "delivered",
            "job_id": job.job_id,
            "decision": ReviewDecision.edit.value,
            "result_id": modified_result_id,
            "response_hash": modified_result_hash,
            "derived_from_result_id": derived_from_result_id,
        }
        self._idempotency_store.store_review_outcome(
            decision_idempotency_key,
            payload_identity,
            {"status_code": 200, "body": body},
            applied=True,
        )
        return (200, body)

    async def _review_reject(
        self,
        job: Job,
        payload_identity: dict,
        decision_idempotency_key: str,
        device: str,
    ) -> tuple[int, dict]:
        # Phase 4A.2.d rule 3: set first-writer guard before any durable emit.
        job.review_decision = ReviewDecision.reject.value
        self._persist_job(job)

        reviewed_evt = event_human_reviewed(
            job_id=job.job_id,
            decision=ReviewDecision.reject.value,
            device=device,
            review_latency_ms=0,
        )
        await self._audit.emit_durable(reviewed_evt)

        fail_evt = event_job_failed(
            job_id=job.job_id,
            error_class="review_reject",
            detail="Rejected via review",
            failing_capability_id=job.governing_capability_id,
        )
        await self._audit.emit_durable(fail_evt)

        job.status = JobStatus.failed.value
        job.error = "review_reject"
        self._persist_job(job)

        body = {
            "status": "failed",
            "job_id": job.job_id,
            "decision": ReviewDecision.reject.value,
            "result_id": job.result_id,
            "response_hash": job.response_hash,
        }
        self._idempotency_store.store_review_outcome(
            decision_idempotency_key,
            payload_identity,
            {"status_code": 200, "body": body},
            applied=True,
        )
        return (200, body)

    async def _review_defer(
        self,
        job: Job,
        payload_identity: dict,
        decision_idempotency_key: str,
        device: str,
    ) -> tuple[int, dict]:
        # Phase 4A.2.d rule 3: defer is non-terminal — do NOT set
        # review_decision. The job remains in proposal_ready and a later
        # review with a fresh decision_idempotency_key is permitted. The
        # defer decision itself is still recorded for idempotency replay.
        reviewed_evt = event_human_reviewed(
            job_id=job.job_id,
            decision=ReviewDecision.defer.value,
            device=device,
            review_latency_ms=0,
        )
        await self._audit.emit_durable(reviewed_evt)

        body = {
            "status": "deferred",
            "job_id": job.job_id,
            "decision": ReviewDecision.defer.value,
            "result_id": job.result_id,
            "response_hash": job.response_hash,
        }
        self._idempotency_store.store_review_outcome(
            decision_idempotency_key,
            payload_identity,
            {"status_code": 200, "body": body},
            applied=True,
        )
        return (200, body)

    async def _review_decline_to_act(
        self,
        job: Job,
        payload_identity: dict,
        decision_idempotency_key: str,
        device: str,
    ) -> tuple[int, dict]:
        # Phase 4A.2.d rule 3: set first-writer guard before any durable emit.
        job.review_decision = ReviewDecision.decline_to_act.value
        # Persist the closed_no_action transition before durable awaits per
        # plan: the lifecycle transition is part of the first-writer state.
        job.status = JobStatus.closed_no_action.value
        self._persist_job(job)

        reviewed_evt = event_human_reviewed(
            job_id=job.job_id,
            decision=ReviewDecision.decline_to_act.value,
            device=device,
            review_latency_ms=0,
        )
        await self._audit.emit_durable(reviewed_evt)

        closed_evt = event_job_closed_no_action(
            job_id=job.job_id,
            result_id=job.result_id,
            response_hash=job.response_hash,
            review_decision=ReviewDecision.decline_to_act.value,
            decision_idempotency_key=decision_idempotency_key,
            governing_capability_id=job.governing_capability_id,
            reason="decline_to_act",
        )
        await self._audit.emit_durable(closed_evt)

        body = {
            "status": "closed_no_action",
            "job_id": job.job_id,
            "decision": ReviewDecision.decline_to_act.value,
            "result_id": job.result_id,
            "response_hash": job.response_hash,
        }
        self._idempotency_store.store_review_outcome(
            decision_idempotency_key,
            payload_identity,
            {"status_code": 200, "body": body},
            applied=True,
        )
        return (200, body)

    async def _spawn_successor(self, original_job: Job, target_capability_id: str) -> Job:
        """Create a successor job for a redirect override."""
        new_job_id = _uuid7()
        now = _now_iso()

        # Reverse-lookup MODEL_MAP by capability_id
        mapping = None
        for _key, entry in MODEL_MAP.items():
            if entry["capability_id"] == target_capability_id:
                mapping = entry
                break

        if mapping is None:
            raise ValueError(f"Unknown capability_id for redirect: {target_capability_id}")

        candidate_models = [mapping["candidate_model"]]
        route_id = mapping["route_id"]
        routing = "local" if target_capability_id == "route.local" else "cloud"

        successor = Job(
            job_id=new_job_id,
            raw_input=original_job.raw_input,
            input_modality=original_job.input_modality,
            device=original_job.device,
            status=JobStatus.classified.value,
            created_at=now,
            classified_at=now,
            parent_job_id=original_job.job_id,
            request_category=original_job.request_category,
            routing_recommendation=routing,
            candidate_models=candidate_models,
            governing_capability_id=target_capability_id,
            confidence=1.0,
        )

        self._jobs[new_job_id] = successor
        self._persist_job(successor)

        # Emit job.classified for the successor (human-directed classification)
        classified_evt = event_job_classified(
            job_id=new_job_id,
            request_category=original_job.request_category or "unknown",
            confidence=1.0,
            local_capable=(routing == "local"),
            routing_recommendation=routing,
            candidate_models=candidate_models,
            governing_capability_id=target_capability_id,
        )
        await self._audit.emit_durable(classified_evt)

        # Enqueue successor for pipeline processing
        try:
            self._queue.put_nowait(new_job_id)
        except asyncio.QueueFull:
            successor.status = JobStatus.failed.value
            successor.error = "pipeline_queue_full"
            self._persist_job(successor)

        return successor

    async def _demote_for_override(
        self,
        capability_id: str | None,
        job_id: str,
        override_type: str,
    ) -> None:
        """Demote the governing capability by 1 level after a cancel/redirect override.

        Uses direct event emission (v1 fallback). Can be replaced with a
        DemotionEngine.handle_override_demotion() call once fully integrated.
        """
        if self._demotion_engine is None:
            return
        if capability_id is None:
            return

        from_level = 0  # v1: all capabilities start at WAL-0
        to_level = from_level - 1  # demote by 1

        event = build_event(
            event_type="wal.demoted",
            job_id=job_id,
            capability_id=capability_id,
            wal_level=to_level,
            payload={
                "capability_id": capability_id,
                "from_level": from_level,
                "to_level": to_level,
                "trigger": "override",
                "override_type": override_type,
            },
        )
        await self._audit.emit_durable(event)
        logger.info(
            "Override demotion: capability %s demoted %d → %d (trigger: override/%s, job: %s)",
            capability_id, from_level, to_level, override_type, job_id,
        )

    # -- auto-accept (Phase 5E) --

    async def _check_auto_accept(self, job: Job) -> None:
        """Auto-accept a WAL-2+ delivered job if the override window has elapsed."""
        if job.status != JobStatus.delivered.value:
            return
        if job.override_type is not None:
            return
        if job.wal_level is None or job.wal_level < 2:
            return
        if job.delivered_at is None:
            return

        delivered_dt = datetime.fromisoformat(job.delivered_at.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - delivered_dt).total_seconds()
        if elapsed < AUTO_ACCEPT_WINDOW_SECONDS:
            return

        # All conditions met — auto-accept
        reviewed_evt = event_human_reviewed(
            job_id=job.job_id,
            decision="auto_delivered",
            device="system",
            review_latency_ms=AUTO_ACCEPT_WINDOW_SECONDS * 1000,
        )
        await self._audit.emit_durable(reviewed_evt)

        job.override_type = "auto_delivered"
        self._persist_job(job)

    async def _auto_accept_loop(self) -> None:
        """Periodically check all jobs for auto-accept eligibility."""
        while True:
            try:
                await asyncio.sleep(AUTO_ACCEPT_POLL_INTERVAL)
            except asyncio.CancelledError:
                return
            for job in list(self._jobs.values()):
                try:
                    await self._check_auto_accept(job)
                except Exception:
                    logger.exception("Auto-accept check failed for job %s", job.job_id)

    # -- background pipeline --

    async def _worker_loop(self) -> None:
        """Process jobs from the queue one at a time."""
        while True:
            job_id = await self._queue.get()
            try:
                # Phase 7F: Wait until hub is in ACTIVE state before processing
                if self._hub_state_manager is not None:
                    while not self._hub_state_manager.is_processing_allowed():
                        await asyncio.sleep(0.25)
                await self._run_pipeline(job_id)
            except Exception:
                logger.exception("Pipeline failed for job %s", job_id)
                job = self._jobs.get(job_id)
                if job and job.status != JobStatus.delivered.value:
                    job.status = JobStatus.failed.value
                    job.error = "pipeline_error"
                    self._persist_job(job)
                # Phase 6E: teardown worker on pipeline failure
                wctx = self._worker_contexts.pop(job_id, None)
                if wctx is not None and self._worker_lifecycle is not None:
                    wctx.status = "failed"
                    try:
                        await self._worker_lifecycle.teardown_worker(wctx)
                    except Exception:
                        logger.exception("Teardown failed for worker %s", wctx.worker_id)
            finally:
                self._queue.task_done()

    async def _run_pipeline(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return

        # 1. Classify (skip for successor jobs that arrive pre-classified)
        if job.status == JobStatus.classified.value and job.governing_capability_id is not None:
            # Successor job — already classified by human override
            routing = job.routing_recommendation
            models = job.candidate_models
            capability_id = job.governing_capability_id
            # Derive route_id from MODEL_MAP
            route_id = None
            for _key, entry in MODEL_MAP.items():
                if entry["capability_id"] == capability_id:
                    route_id = entry["route_id"]
                    break
            if route_id is None:
                route_id = "unknown"
        else:
            classification = await classify(job.raw_input)
            category = classification.category
            routing = classification.routing
            models = classification.candidate_models
            capability_id = classification.capability_id
            route_id = classification.route_id

            event = event_job_classified(
                job_id=job_id,
                request_category=category,
                confidence=classification.confidence,
                local_capable=classification.local_capable,
                routing_recommendation=routing,
                candidate_models=models,
                governing_capability_id=capability_id,
            )
            await self._audit.emit_durable(event)

            job.status = JobStatus.classified.value
            job.classified_at = _now_iso()
            job.request_category = category
            job.routing_recommendation = routing
            job.candidate_models = models
            job.governing_capability_id = capability_id
            job.confidence = classification.confidence

        # Phase 5E: record WAL level at classification time (v1: all WAL-0)
        job.wal_level = 0
        self._persist_job(job)

        # 1.5. Circuit breaker gate — block dispatch if route is OPEN
        if (
            routing == "cloud"
            and self._connectivity_monitor is not None
            and not self._connectivity_monitor.is_route_available(route_id)
        ):
            queued_evt = event_job_queued(
                job_id=job_id, reason="connectivity", position=0, estimated_wait_ms=None,
            )
            await self._audit.emit_durable(queued_evt)
            failed_evt = event_job_failed(
                job_id=job_id,
                error_class="route_unavailable",
                detail=f"Circuit breaker OPEN for route {route_id}",
            )
            await self._audit.emit_durable(failed_evt)
            job.status = JobStatus.failed.value
            job.error = f"route_unavailable: circuit breaker OPEN for {route_id}"
            self._persist_job(job)
            logger.warning(
                "Job %s blocked: circuit breaker OPEN for route %s", job_id, route_id,
            )
            return

        # 2. Permission check
        # Phase 4A.2.b: capture the result-holding signal (delivery_hold or
        # on_accept) so the response/result boundary can transition the job
        # into proposal_ready instead of silently delivering.
        perm_delivery_hold = False
        perm_hold_type: str | None = None
        if self._permission_checker:
            # Determine the dispatch action based on routing
            if routing == "local":
                dispatch_action = "dispatch_local"
            elif capability_id == "route.multi":
                dispatch_action = "dispatch_multi"
            else:
                dispatch_action = "dispatch_cloud"

            job_ctx = JobContext(
                job_id=job_id,
                est_cost=None,  # Cost not known until after dispatch
            )

            perm_result = await self._permission_checker.check_permission(
                capability_id=capability_id,
                action=dispatch_action,
                job_ctx=job_ctx,
            )
            perm_source_event_id = perm_result.source_event_id
            perm_delivery_hold = bool(perm_result.delivery_hold)
            perm_hold_type = perm_result.hold_type

            if perm_result.result == "blocked":
                failed_event = event_job_failed(
                    job_id=job_id,
                    error_class="permission_denied",
                    detail=f"Action '{dispatch_action}' blocked: {perm_result.block_reason}",
                )
                await self._audit.emit_durable(failed_event)
                job.status = JobStatus.failed.value
                job.error = f"permission_denied: {perm_result.block_reason}"
                self._persist_job(job)
                return

            if perm_result.result == "held":
                # pre_action and cost_approval holds fire before a result
                # artifact exists; per Phase 4A.2.b they are not
                # proposal_ready triggers and the v1 stub-mode behavior
                # (proceed) is preserved here. on_accept is captured above
                # and surfaced at the response/result boundary.
                logger.info(
                    "Job %s action '%s' held (%s) — proceeding in v1 stub mode",
                    job_id, dispatch_action, perm_result.hold_type,
                )
        else:
            # Fallback to legacy stub
            perm_event = event_wal_permission_check(
                job_id=job_id,
                capability_id=capability_id,
                requested_action="dispatch",
                current_level=0,
                required_level=0,
                result="allowed",
                delivery_hold=False,
            )
            perm_source_event_id = perm_event["source_event_id"]
            await self._audit.emit_durable(perm_event)

        # 2.5. Worker preparation (Phase 6E): prepare worker context
        # after permission check, before context packaging.
        worker_ctx: Optional[WorkerContext] = None
        if self._worker_lifecycle is not None:
            try:
                worker_ctx = await self._worker_lifecycle.prepare_worker(job)
                self._worker_contexts[job_id] = worker_ctx
                worker_ctx.status = "active"
            except Exception as exc:
                logger.error("Worker preparation failed for job %s: %s", job_id, exc)
                job.status = JobStatus.failed.value
                job.error = "worker_preparation_failed"
                self._persist_job(job)
                return

        # 2b. Egress fallback check: if routing locally, check whether
        # any cloud routes for this capability are disabled. If so,
        # emit egress.fallback_to_local for auditability.
        if routing == "local" and self._egress_registry is not None:
            blocked_routes = self._check_blocked_cloud_routes(capability_id)
            if blocked_routes:
                fallback_event = event_egress_fallback_to_local(
                    job_id=job_id,
                    blocked_route_ids=blocked_routes,
                    classifier_routing=routing,
                    capability_id=capability_id,
                )
                await self._audit.emit_durable(fallback_event)

        # 3. Context packaging (cloud routes only)
        context_package_id = None
        assembled_payload_hash = None

        if routing == "cloud" and self._packager is not None:
            pkg_result = await self._packager.package(
                job_id=job_id,
                raw_input=job.raw_input,
                target_model=models[0],
                route_id=route_id,
                capability_id=capability_id,
                wal_level=0,
            )
            context_package_id = pkg_result.context_package_id
            assembled_payload_hash = pkg_result.assembled_payload_hash
            dispatch_prompt = pkg_result.assembled_payload
        elif routing == "cloud" and self._packager is None:
            raise RuntimeError(
                "Context packager is not initialized — cannot dispatch cloud job "
                f"{job_id} without context packaging (fail-closed)"
            )
        else:
            dispatch_prompt = job.raw_input

        # 4. Dispatch
        dispatch_event = event_job_dispatched(
            job_id=job_id,
            target_model=models[0],
            route_id=route_id,
            wal_permission_check_ref=perm_source_event_id,
            context_package_id=context_package_id,
            assembled_payload_hash=assembled_payload_hash or "",
        )
        await self._audit.emit_durable(dispatch_event)

        job.status = JobStatus.dispatched.value
        job.dispatched_at = _now_iso()
        self._persist_job(job)

        dispatch_start = time.monotonic()

        if routing == "local":
            if (
                worker_ctx is not None
                and self._worker_lifecycle is not None
                and hasattr(self._worker_lifecycle, "has_executor")
                and self._worker_lifecycle.has_executor
            ):
                # Branch 1: worker container path
                try:
                    worker_result = await self._worker_lifecycle.execute_in_worker(
                        context=worker_ctx,
                        prompt=dispatch_prompt,
                        model=models[0],
                        task_type="text_generation",
                    )
                    response_text = worker_result["response_text"]
                    latency_ms = worker_result["latency_ms"]
                    token_in = worker_result["token_count_in"]
                    token_out = worker_result["token_count_out"]
                    cost_usd = 0.0
                    result_id = _uuid7()
                    response_hash = hashlib.sha256(response_text.encode()).hexdigest()[:16]
                    finish_reason = "stop"
                except Exception as exc:
                    logger.error("Worker execution failed for job %s: %s", job_id, exc)
                    failed_event = event_job_failed(
                        job_id=job_id,
                        error_class="worker_execution_failed",
                        detail=str(exc),
                    )
                    await self._audit.emit_durable(failed_event)
                    job.status = JobStatus.failed.value
                    job.error = "worker_execution_failed"
                    self._persist_job(job)
                    if worker_ctx is not None and self._worker_lifecycle is not None:
                        worker_ctx.status = "failed"
                        await self._worker_lifecycle.teardown_worker(worker_ctx)
                        self._worker_contexts.pop(job_id, None)
                    return
            else:
                # Branch 2: fallback to local generate
                local_result = await generate_local_response(dispatch_prompt)
                response_text = local_result.text
                latency_ms = int((time.monotonic() - dispatch_start) * 1000)
                token_in = local_result.token_count_in
                token_out = local_result.token_count_out
                cost_usd = 0.0
                result_id = _uuid7()
                response_hash = hashlib.sha256(response_text.encode()).hexdigest()[:16]
                finish_reason = "stop"
        else:
            # Cloud dispatch via egress gateway
            async with httpx.AsyncClient(timeout=60.0) as client:
                egress_resp = await client.post(
                    f"{EGRESS_GATEWAY_URL}/dispatch",
                    json={
                        "job_id": job_id,
                        "route_id": route_id,
                        "capability_id": capability_id,
                        "target_model": models[0],
                        "prompt": dispatch_prompt,
                        "assembled_payload_hash": assembled_payload_hash or "",
                        "wal_permission_check_ref": perm_source_event_id,
                    },
                )
            egress_data = egress_resp.json()
            if egress_data.get("status") in ("blocked", "error"):
                failure_type = egress_data.get("failure_type", "unknown")
                detail = egress_data.get("detail", "")
                failed_event = event_job_failed(
                    job_id=job_id,
                    error_class=failure_type,
                    detail=detail,
                    failing_capability_id=failure_type,
                )
                await self._audit.emit_durable(failed_event)
                job.status = JobStatus.failed.value
                job.error = f"{failure_type}: {detail}"
                job.last_failing_capability_id = failure_type
                self._persist_job(job)
                # Phase 6E: teardown worker on dispatch failure
                if worker_ctx is not None and self._worker_lifecycle is not None:
                    worker_ctx.status = "failed"
                    await self._worker_lifecycle.teardown_worker(worker_ctx)
                    self._worker_contexts.pop(job_id, None)
                return
            response_text = egress_data.get("response_text", "")
            latency_ms = egress_data.get("latency_ms", 0)
            token_in = egress_data.get("token_count_in", 0)
            token_out = egress_data.get("token_count_out", 0)
            cost_usd = egress_data.get("cost_estimate_usd", 0.0)
            result_id = egress_data.get("result_id", _uuid7())
            response_hash = egress_data.get("response_hash", "")
            finish_reason = egress_data.get("finish_reason", "stop")

        # model.response
        resp_event = event_model_response(
            job_id=job_id,
            model=models[0],
            route_id=route_id,
            latency_ms=latency_ms,
            token_count_in=token_in,
            token_count_out=token_out,
            cost_estimate_usd=cost_usd,
            result_id=result_id,
            response_hash=response_hash,
            finish_reason=finish_reason,
        )
        await self._audit.emit_durable(resp_event)

        # job.response_received
        recv_event = event_job_response_received(
            job_id=job_id,
            model=models[0],
            latency_ms=latency_ms,
            token_count_in=token_in,
            token_count_out=token_out,
            cost_estimate_usd=cost_usd,
            result_id=result_id,
            response_hash=response_hash,
        )
        await self._audit.emit_durable(recv_event)

        job.status = JobStatus.response_received.value
        job.response_received_at = _now_iso()
        job.result = response_text
        job.result_id = result_id
        job.response_hash = response_hash
        self._persist_job(job)

        # Write original result to disk for lineage
        _write_result(result_id, response_text)

        # Phase 4A.2.b: result-holding review-gate outcomes (delivery_hold or
        # post-result on_accept) transition the job to proposal_ready and
        # halt the pipeline before delivery. A durable job.proposal_ready
        # event records the held-result decision in the audit log.
        hold_reason = self._derive_proposal_hold_reason(
            delivery_hold=perm_delivery_hold,
            hold_type=perm_hold_type,
        )
        if hold_reason is not None:
            if job.proposal_id is None:
                job.proposal_id = _uuid7()
            job.status = JobStatus.proposal_ready.value
            self._persist_job(job)

            proposal_event = event_job_proposal_ready(
                job_id=job_id,
                proposal_id=job.proposal_id,
                result_id=result_id,
                response_hash=response_hash,
                proposed_by=models[0],
                governing_capability_id=capability_id,
                confidence=job.confidence,
                auto_accept_at=None,
                hold_reason=hold_reason,
            )
            await self._audit.emit_durable(proposal_event)

            # Phase 6E: teardown worker on proposal_ready (no further
            # pipeline work happens until a review decision arrives).
            if worker_ctx is not None and self._worker_lifecycle is not None:
                await self._worker_lifecycle.teardown_worker(worker_ctx)
                self._worker_contexts.pop(job_id, None)

            logger.info(
                "Job %s held for review (proposal_ready, hold_reason=%s)",
                job_id, hold_reason,
            )
            return

        # 4. Deliver
        deliver_event = event_job_delivered(job_id=job_id)
        await self._audit.emit_durable(deliver_event)

        job.status = JobStatus.delivered.value
        job.delivered_at = _now_iso()
        self._persist_job(job)

        # Phase 6E: teardown worker on job completion
        if worker_ctx is not None and self._worker_lifecycle is not None:
            await self._worker_lifecycle.teardown_worker(worker_ctx)
            self._worker_contexts.pop(job_id, None)

        logger.info("Job %s delivered successfully", job_id)

    @staticmethod
    def _derive_proposal_hold_reason(
        *,
        delivery_hold: bool,
        hold_type: str | None,
    ) -> str | None:
        """Map the captured permission outcome to a Phase 4A.2.b hold_reason.

        Authorized triggers:
            - delivery_hold == True       -> "pre_delivery"
            - hold_type   == "on_accept"  -> "on_accept" (only valid once a
              result exists; this helper is invoked at the response/result
              boundary so that precondition is structurally satisfied)

        pre_action and cost_approval hold types are not proposal_ready
        triggers and yield None.
        """
        if delivery_hold:
            return "pre_delivery"
        if hold_type == "on_accept":
            return "on_accept"
        return None

    def get_proposal(self, job_id: str) -> Optional[Proposal]:
        """Return a Proposal for a proposal_ready job, otherwise None.

        Centralizes proposal derivation so the HTTP route does not duplicate
        proposal-population rules. proposed_by is the producing model
        identifier captured at proposal time (candidate_models[0]) per the
        2026-04-26 plan amendment. auto_accept_at is None in v1.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status != JobStatus.proposal_ready.value:
            return None
        if (
            job.proposal_id is None
            or job.result_id is None
            or job.response_hash is None
            or job.governing_capability_id is None
            or job.confidence is None
            or not job.candidate_models
        ):
            return None
        return Proposal(
            proposal_id=job.proposal_id,
            job_id=job.job_id,
            result_id=job.result_id,
            response_hash=job.response_hash,
            proposed_by=job.candidate_models[0],
            governing_capability_id=job.governing_capability_id,
            confidence=job.confidence,
            auto_accept_at=None,
        )

    def list_jobs(
        self,
        status: "JobStatus | str",
        since: Optional[str],
        limit: int,
    ) -> JobListResponse:
        """Phase 4A.2.e: list jobs by status with UUIDv7 cursor pagination.

        Reads from the in-memory job cache only. Newest-first ordering by
        UUIDv7 job_id descending. since is an exclusive UUIDv7 boundary —
        jobs whose job_id < since are returned. The since value need not be
        present in the filtered set. next_cursor is the job_id of the last
        returned item when more matching jobs remain after this page,
        otherwise None.
        """
        status_value = status.value if isinstance(status, JobStatus) else status

        matching = sorted(
            (j for j in self._jobs.values() if j.status == status_value),
            key=lambda j: j.job_id,
            reverse=True,
        )

        if since is not None:
            matching = [j for j in matching if j.job_id < since]

        page = matching[:limit]
        has_more = len(matching) > limit
        next_cursor = page[-1].job_id if (has_more and page) else None

        items: list[JobStatusResponse] = []
        for job in page:
            proposal = self.get_proposal(job.job_id)
            items.append(
                JobStatusResponse(
                    job_id=job.job_id,
                    status=job.status,
                    created_at=job.created_at,
                    classified_at=job.classified_at,
                    dispatched_at=job.dispatched_at,
                    response_received_at=job.response_received_at,
                    delivered_at=job.delivered_at,
                    request_category=job.request_category,
                    routing_recommendation=job.routing_recommendation,
                    result=job.result,
                    error=job.error,
                    proposal=proposal,
                )
            )

        return JobListResponse(
            items=items,
            next_cursor=next_cursor,
            count=len(items),
        )

    def _check_blocked_cloud_routes(self, capability_id: str) -> list[str]:
        """Return route_ids of disabled cloud routes that list this capability.

        Only checks cloud routes (provider != 'ollama'). Returns an empty
        list if all relevant cloud routes are enabled or if no registry
        is available.
        """
        if self._egress_registry is None:
            return []

        blocked: list[str] = []
        for route in self._egress_registry.all_routes():
            if route.provider == "ollama":
                continue
            if capability_id in route.allowed_capabilities and not route.enabled:
                blocked.append(route.route_id)
        return blocked
