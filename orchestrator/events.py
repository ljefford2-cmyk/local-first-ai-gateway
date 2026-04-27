"""Event construction helpers for the DRNT orchestrator."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

import uuid_utils


def _now_iso() -> str:
    """ISO 8601 UTC timestamp with microsecond precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uuid7() -> str:
    """Generate a UUIDv7 string."""
    return str(uuid_utils.uuid7())


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def build_event(
    *,
    event_type: str,
    job_id: Optional[str],
    durability: str = "durable",
    payload: dict[str, Any],
    capability_id: Optional[str] = None,
    wal_level: Optional[int] = None,
    parent_job_id: Optional[str] = None,
    source: str = "orchestrator",
) -> dict[str, Any]:
    """Build a fully populated event envelope for the audit log writer."""
    return {
        "schema_version": "2.0",
        "source_event_id": _uuid7(),
        "timestamp": _now_iso(),
        "event_type": event_type,
        "job_id": job_id,
        "parent_job_id": parent_job_id,
        "capability_id": capability_id,
        "wal_level": wal_level,
        "source": source,
        "durability": durability,
        "payload": payload,
    }


# ---------- Phase 2 event builders ----------


def event_system_startup(
    config_hash: str | None = None,
    state_hash: str | None = None,
    ollama_model: str | None = None,
    ollama_model_version: str | None = None,
    components_loaded: list[str] | None = None,
    recovery_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full system.startup event with hashes and component info."""
    payload = {
        "component": "orchestrator",
        "version": "0.4.0",
        "config_hash": config_hash,
        "state_hash": state_hash,
        "ollama_model": ollama_model,
        "ollama_model_version": ollama_model_version,
        "components_loaded": components_loaded or [],
    }
    if recovery_summary is not None:
        payload["recovery_summary"] = recovery_summary
    return build_event(
        event_type="system.startup",
        job_id=None,
        payload=payload,
    )


def event_wal_promoted(
    capability_id: str,
    capability_name: str,
    from_level: int,
    to_level: int,
    criteria_met: dict | None = None,
    approved_by: str = "human",
) -> dict[str, Any]:
    """Build a wal.promoted event per Event Schema §3.3."""
    return build_event(
        event_type="wal.promoted",
        job_id=None,
        capability_id=capability_id,
        wal_level=to_level,
        payload={
            "capability_id": capability_id,
            "capability_name": capability_name,
            "from_level": from_level,
            "to_level": to_level,
            "criteria_met": criteria_met or {},
            "approved_by": approved_by,
        },
    )


def event_system_config_change(
    previous_hash: str | None,
    new_hash: str,
    changed_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Build a system.config_change event."""
    return build_event(
        event_type="system.config_change",
        job_id=None,
        payload={
            "previous_hash": previous_hash,
            "new_hash": new_hash,
            "changed_capabilities": changed_capabilities or [],
        },
    )


def event_system_model_change(
    previous_model: str | None,
    new_model: str,
    affected_capabilities: list[str],
) -> dict[str, Any]:
    """Build a system.model_change event."""
    return build_event(
        event_type="system.model_change",
        job_id=None,
        payload={
            "previous_model": previous_model,
            "new_model": new_model,
            "affected_capabilities": affected_capabilities,
        },
    )


def event_job_submitted(
    job_id: str,
    raw_input: str,
    input_modality: str,
    device: str,
) -> dict[str, Any]:
    return build_event(
        event_type="job.submitted",
        job_id=job_id,
        payload={
            "raw_input": raw_input,
            "input_modality": input_modality,
            "device": device,
            "request_category": None,
        },
    )


def event_job_classified(
    job_id: str,
    request_category: str,
    confidence: float,
    local_capable: bool,
    routing_recommendation: str,
    candidate_models: list[str],
    governing_capability_id: str,
) -> dict[str, Any]:
    return build_event(
        event_type="job.classified",
        job_id=job_id,
        capability_id=governing_capability_id,
        payload={
            "request_category": request_category,
            "confidence": confidence,
            "local_capable": local_capable,
            "routing_recommendation": routing_recommendation,
            "candidate_models": candidate_models,
            "governing_capability_id": governing_capability_id,
            "declared_pipeline": [
                {"capability_id": governing_capability_id, "dependency_mode": "required"}
            ],
        },
    )


def event_wal_permission_check(
    job_id: str,
    capability_id: str,
    requested_action: str,
    current_level: int,
    required_level: int | None,
    result: str,
    delivery_hold: bool,
    hold_type: str | None = None,
    block_reason: str | None = None,
    dependency_mode: str | None = None,
    wal_level: int | None = None,
) -> dict[str, Any]:
    """Build a full wal.permission_check event per Event Schema S3.3."""
    return build_event(
        event_type="wal.permission_check",
        job_id=job_id,
        capability_id=capability_id,
        wal_level=wal_level if wal_level is not None else current_level,
        payload={
            "capability_id": capability_id,
            "job_id": job_id,
            "requested_action": requested_action,
            "current_level": current_level,
            "required_level": required_level,
            "result": result,
            "delivery_hold": delivery_hold,
            "hold_type": hold_type,
            "block_reason": block_reason,
            "dependency_mode": dependency_mode,
        },
    )


def event_wal_demoted(
    capability_id: str,
    capability_name: str,
    from_level: int,
    to_level: int,
    trigger: str,
    incident_ref: str | None = None,
) -> dict[str, Any]:
    """Build a wal.demoted event per Event Schema §3.3.

    trigger must be one of:
        error, anomaly, override, model_change, manual, config_reconcile, strip_failure
    """
    return build_event(
        event_type="wal.demoted",
        job_id=None,
        capability_id=capability_id,
        wal_level=to_level,
        payload={
            "capability_id": capability_id,
            "capability_name": capability_name,
            "from_level": from_level,
            "to_level": to_level,
            "trigger": trigger,
            "incident_ref": incident_ref,
        },
    )


def event_job_dispatched(
    job_id: str,
    target_model: str,
    route_id: str,
    wal_permission_check_ref: str,
    assembled_payload_hash: str,
    egress_config_hash: Optional[str] = None,
    context_package_id: Optional[str] = None,
) -> dict[str, Any]:
    return build_event(
        event_type="job.dispatched",
        job_id=job_id,
        payload={
            "target_model": target_model,
            "route_id": route_id,
            "context_package_id": context_package_id,
            "wal_permission_check_ref": wal_permission_check_ref,
            "retry_sequence": 0,
            "same_capability_retry": True,
            "assembled_payload_hash": assembled_payload_hash,
            "egress_config_hash": egress_config_hash,
        },
    )


def event_model_response(
    job_id: str,
    model: str,
    route_id: str,
    latency_ms: int,
    token_count_in: int,
    token_count_out: int,
    cost_estimate_usd: float,
    result_id: str,
    response_hash: str,
    finish_reason: str,
) -> dict[str, Any]:
    return build_event(
        event_type="model.response",
        job_id=job_id,
        payload={
            "model": model,
            "route_id": route_id,
            "latency_ms": latency_ms,
            "token_count_in": token_count_in,
            "token_count_out": token_count_out,
            "cost_estimate_usd": cost_estimate_usd,
            "result_id": result_id,
            "response_hash": response_hash,
            "finish_reason": finish_reason,
        },
    )


def event_job_response_received(
    job_id: str,
    model: str,
    latency_ms: int,
    token_count_in: int,
    token_count_out: int,
    cost_estimate_usd: float,
    result_id: str,
    response_hash: str,
) -> dict[str, Any]:
    return build_event(
        event_type="job.response_received",
        job_id=job_id,
        payload={
            "model": model,
            "latency_ms": latency_ms,
            "token_count_in": token_count_in,
            "token_count_out": token_count_out,
            "cost_estimate_usd": cost_estimate_usd,
            "result_id": result_id,
            "response_hash": response_hash,
        },
    )


def event_job_delivered(job_id: str) -> dict[str, Any]:
    return build_event(
        event_type="job.delivered",
        job_id=job_id,
        payload={
            "delivery_target": "api_response",
            "delivery_method": "poll",
        },
    )


def event_system_notify(
    notification_type: str,
    title: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """Build a system.notify event for operator notifications.

    Used for promotion recommendations, skill failure advisories, etc.
    """
    return build_event(
        event_type="system.notify",
        job_id=None,
        source="system",
        payload={
            "notification_type": notification_type,
            "title": title,
            "detail": detail,
        },
    )


# ---------- Phase 3 event builders ----------


def event_egress_validation_failure(
    job_id: str,
    failure_type: str,
    detail: str,
    route_id: str,
) -> dict[str, Any]:
    return build_event(
        event_type="egress.validation_failure",
        job_id=job_id,
        payload={
            "context_package_id": None,
            "job_id": job_id,
            "failure_type": failure_type,
            "detail": detail,
            "route_id": route_id,
            "failing_component": "egress_gateway",
            "blocked": True,
        },
    )


def event_egress_fallback_to_local(
    job_id: str,
    blocked_route_ids: list[str],
    classifier_routing: str,
    capability_id: str,
) -> dict[str, Any]:
    return build_event(
        event_type="egress.fallback_to_local",
        job_id=job_id,
        capability_id=capability_id,
        payload={
            "blocked_route_ids": blocked_route_ids,
            "classifier_routing": classifier_routing,
            "capability_id": capability_id,
            "detail": (
                "Classifier routed locally; cloud route(s) disabled in egress config. "
                "Local dispatch proceeded. Logged for audit trail."
            ),
        },
    )


def event_model_error(
    job_id: str,
    model: str,
    route_id: str,
    error_type: str,
    error_detail: str,
    latency_ms: int,
) -> dict[str, Any]:
    return build_event(
        event_type="model.error",
        job_id=job_id,
        payload={
            "model": model,
            "route_id": route_id,
            "error_type": error_type,
            "error_detail": error_detail,
            "latency_ms": latency_ms,
            "retry_sequence": 0,
        },
    )


def event_job_failed(
    job_id: str,
    error_class: str,
    detail: str,
    failing_capability_id: str | None = None,
) -> dict[str, Any]:
    """Build a job.failed event.

    error_class is a free string. Known values include:
        override_cancel, override_redirect, permission_denied,
        worker_preparation_failed, recovery_exhausted
    """
    return build_event(
        event_type="job.failed",
        job_id=job_id,
        payload={
            "error_class": error_class,
            "detail": detail,
            "failing_capability_id": failing_capability_id,
        },
    )


# ---------- Phase 4 context packager event builders ----------


def event_context_packaged(
    context_package_id: str,
    job_id: str,
    fields_included: list[str],
    fields_stripped: list[str],
    outbound_token_estimate: int,
    assembled_payload_hash: str,
    context_object_hash: str,
) -> dict[str, Any]:
    return build_event(
        event_type="context.packaged",
        job_id=job_id,
        source="context_packager",
        payload={
            "context_package_id": context_package_id,
            "job_id": job_id,
            "fields_included": fields_included,
            "fields_stripped": fields_stripped,
            "fields_blocked": [],
            "memory_objects_attached": 0,
            "memory_retrieval_ms": 0,
            "outbound_token_estimate": outbound_token_estimate,
            "assembled_payload_hash": assembled_payload_hash,
            "context_object_hash": context_object_hash,
            "exclusion_summary": {
                "local_only": 0,
                "below_confidence": 0,
                "over_budget": 0,
            },
        },
    )


# ---------- Phase 5 override event builders ----------


def event_human_override(
    job_id: str,
    override_type: str,
    target: str,
    detail: str,
    device: str,
) -> dict[str, Any]:
    """Build a human.override event — durable, source 'human'."""
    return build_event(
        event_type="human.override",
        job_id=job_id,
        source="human",
        payload={
            "job_id": job_id,
            "override_type": override_type,
            "target": target,
            "detail": detail,
            "device": device,
        },
    )


def event_job_revoked(
    job_id: str,
    reason: str,
    override_source_event_id: str,
    successor_job_id: str | None = None,
    memory_objects_superseded: list[str] | None = None,
) -> dict[str, Any]:
    """Build a job.revoked event — durable, source 'orchestrator'."""
    return build_event(
        event_type="job.revoked",
        job_id=job_id,
        payload={
            "reason": reason,
            "override_source_event_id": override_source_event_id,
            "successor_job_id": successor_job_id,
            "memory_objects_superseded": memory_objects_superseded if memory_objects_superseded is not None else [],
        },
    )


def event_job_proposal_ready(
    job_id: str,
    proposal_id: str,
    result_id: str,
    response_hash: str,
    proposed_by: str,
    governing_capability_id: str,
    confidence: float | None,
    auto_accept_at: str | None,
    hold_reason: str,
) -> dict[str, Any]:
    """Build a job.proposal_ready event — durable lifecycle transition.

    Emitted after the result/response artifact has been recorded and before
    the pipeline stops for human review. Records the held-result decision
    in the audit log so pending review state is durably attributable even
    if no human.reviewed event is ever emitted.

    hold_reason is one of: "pre_delivery" | "on_accept".
    """
    return build_event(
        event_type="job.proposal_ready",
        job_id=job_id,
        capability_id=governing_capability_id,
        payload={
            "proposal_id": proposal_id,
            "result_id": result_id,
            "response_hash": response_hash,
            "proposed_by": proposed_by,
            "governing_capability_id": governing_capability_id,
            "confidence": confidence,
            "auto_accept_at": auto_accept_at,
            "hold_reason": hold_reason,
        },
    )


def event_job_closed_no_action(
    job_id: str,
    result_id: str,
    response_hash: str,
    review_decision: str,
    decision_idempotency_key: str,
    governing_capability_id: str | None,
    reason: str,
) -> dict[str, Any]:
    """Build a job.closed_no_action event — durable lifecycle transition.

    Emitted after a winning decline_to_act review decision is recorded and
    before the review handler returns its final response. Records the
    lifecycle transition into closed_no_action separately from
    human.reviewed so the transition is durably attributable in the audit
    log independently of the human.reviewed decision string.
    """
    return build_event(
        event_type="job.closed_no_action",
        job_id=job_id,
        capability_id=governing_capability_id,
        payload={
            "result_id": result_id,
            "response_hash": response_hash,
            "review_decision": review_decision,
            "decision_idempotency_key": decision_idempotency_key,
            "governing_capability_id": governing_capability_id,
            "reason": reason,
        },
    )


def event_human_reviewed(
    job_id: str,
    decision: str,
    device: str,
    review_latency_ms: int,
    modification_summary: str | None = None,
    modified_result_id: str | None = None,
    modified_result_hash: str | None = None,
    derived_from_result_id: str | None = None,
) -> dict[str, Any]:
    """Build a human.reviewed event — durable, source 'human'.

    Per Event Schema doc Section 4.5.
    decision: accepted | modified | rejected | resubmitted
    """
    return build_event(
        event_type="human.reviewed",
        job_id=job_id,
        source="human",
        payload={
            "job_id": job_id,
            "decision": decision,
            "device": device,
            "review_latency_ms": review_latency_ms,
            "modification_summary": modification_summary,
            "modified_result_id": modified_result_id,
            "modified_result_hash": modified_result_hash,
            "derived_from_result_id": derived_from_result_id,
        },
    )


# ---------- Phase 6A manifest event builders ----------


def event_manifest_validated(
    manifest_id: str,
    capability_id: str,
    worker_type: str,
    valid: bool,
    error_count: int,
    warning_count: int,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Build a manifest.validated event per Spec 6A.

    Emitted after every manifest validation attempt, whether it passes or fails.
    """
    payload: dict[str, Any] = {
        "manifest_id": manifest_id,
        "capability_id": capability_id,
        "worker_type": worker_type,
        "valid": valid,
        "error_count": error_count,
        "warning_count": warning_count,
    }
    if not valid and errors:
        payload["errors"] = errors
    return build_event(
        event_type="manifest.validated",
        job_id=None,
        source="orchestrator",
        payload=payload,
    )


def event_manifest_rejected(
    manifest_id: str,
    capability_id: str,
    rejection_reason: str,
    all_errors: list[str],
) -> dict[str, Any]:
    """Build a manifest.rejected event per Spec 6A.

    Emitted when a manifest fails validation. rejection_reason is the first error.
    """
    return build_event(
        event_type="manifest.rejected",
        job_id=None,
        source="orchestrator",
        payload={
            "manifest_id": manifest_id,
            "capability_id": capability_id,
            "rejection_reason": rejection_reason,
            "all_errors": all_errors,
        },
    )


def event_hub_startup_validated(
    hub_start_permitted: bool,
    checks_passed: int,
    checks_failed: int,
    critical_failures: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    """Build a hub.startup_validated event per Spec 6D.

    Emitted after all startup validation checks complete successfully
    (hub_start_permitted is True).
    """
    return build_event(
        event_type="hub.startup_validated",
        job_id=None,
        source="orchestrator",
        payload={
            "hub_start_permitted": hub_start_permitted,
            "checks_passed": checks_passed,
            "checks_failed": checks_failed,
            "critical_failures": critical_failures,
            "warnings": warnings,
        },
    )


def event_hub_startup_blocked(
    blocking_checks: list[str],
    message: str,
) -> dict[str, Any]:
    """Build a hub.startup_blocked event per Spec 6D.

    Emitted when hub_start_permitted is False — at least one critical
    check failed and the hub must not start.
    """
    return build_event(
        event_type="hub.startup_blocked",
        job_id=None,
        source="orchestrator",
        payload={
            "blocking_checks": blocking_checks,
            "message": message,
        },
    )


def event_context_strip_detail(
    context_package_id: str,
    job_id: str,
    field_id: str,
    field_name: str,
    span_start: int,
    span_end: int,
    original_class: str,
    action: str,
    reason: str,
) -> dict[str, Any]:
    return build_event(
        event_type="context.strip_detail",
        job_id=job_id,
        source="context_packager",
        payload={
            "context_package_id": context_package_id,
            "field_id": field_id,
            "field_name": field_name,
            "span": {"start_char": span_start, "end_char": span_end},
            "original_class": original_class,
            "classification_status": "matched",
            "action": action,
            "method": "redact" if action == "stripped" else "generalize",
            "detected_by": "packager",
            "failing_capability_id": None,
            "reason": reason,
        },
    )


# ---------- Phase 6B blueprint event builders ----------


def event_blueprint_created(
    blueprint_id: str,
    manifest_id: str,
    capability_id: str,
    network_mode: str,
    mount_count: int,
    cap_drop_count: int,
    memory_limit: str,
) -> dict[str, Any]:
    """Build a sandbox.blueprint_created event per Spec 6B.

    Emitted when a SandboxBlueprint is successfully generated from a
    validated RuntimeManifest.
    """
    return build_event(
        event_type="sandbox.blueprint_created",
        job_id=None,
        source="orchestrator",
        payload={
            "blueprint_id": blueprint_id,
            "manifest_id": manifest_id,
            "capability_id": capability_id,
            "network_mode": network_mode,
            "mount_count": mount_count,
            "cap_drop_count": cap_drop_count,
            "memory_limit": memory_limit,
        },
    )


# ---------- Phase 6E worker lifecycle event builders ----------


def event_worker_prepared(
    worker_id: str,
    job_id: str,
    capability_id: str,
    blueprint_id: str,
    manifest_id: str,
) -> dict[str, Any]:
    """Build a worker.prepared event per Spec 6E.

    Emitted when a WorkerContext is successfully created for a job.
    """
    return build_event(
        event_type="worker.prepared",
        job_id=job_id,
        capability_id=capability_id,
        source="orchestrator",
        payload={
            "worker_id": worker_id,
            "job_id": job_id,
            "capability_id": capability_id,
            "blueprint_id": blueprint_id,
            "manifest_id": manifest_id,
        },
    )


def event_worker_teardown(
    worker_id: str,
    job_id: str,
    total_egress_requests: int,
    denied_egress_requests: int,
    duration_seconds: float,
) -> dict[str, Any]:
    """Build a worker.teardown event per Spec 6E.

    Emitted when a worker context is cleaned up after job completion.
    """
    return build_event(
        event_type="worker.teardown",
        job_id=job_id,
        source="orchestrator",
        payload={
            "worker_id": worker_id,
            "job_id": job_id,
            "total_egress_requests": total_egress_requests,
            "denied_egress_requests": denied_egress_requests,
            "duration_seconds": duration_seconds,
        },
    )


def event_worker_preparation_failed(
    job_id: str,
    capability_id: str,
    error: str,
) -> dict[str, Any]:
    """Build a job.failed event with error_class worker_preparation_failed."""
    return build_event(
        event_type="job.failed",
        job_id=job_id,
        capability_id=capability_id,
        source="orchestrator",
        payload={
            "error_class": "worker_preparation_failed",
            "detail": error,
            "failing_capability_id": capability_id,
        },
    )


# ---------- Worker Execution event builders (Priority #5) ----------


def event_worker_execution_started(
    worker_id: str,
    job_id: str,
    capability_id: str,
    container_id: str,
    image: str,
    task_type: str,
) -> dict[str, Any]:
    """Build a worker.execution_started event.

    Emitted when a worker container begins executing a task.
    task_type matches the task.json task_type field (e.g. 'text_generation').
    """
    return build_event(
        event_type="worker.execution_started",
        job_id=job_id,
        capability_id=capability_id,
        source="orchestrator",
        payload={
            "worker_id": worker_id,
            "job_id": job_id,
            "capability_id": capability_id,
            "container_id": container_id,
            "image": image,
            "task_type": task_type,
        },
    )


def event_worker_execution_completed(
    worker_id: str,
    job_id: str,
    capability_id: str,
    container_id: str,
    exit_code: int,
    success: bool,
    latency_ms: int,
    token_count_in: int = 0,
    token_count_out: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    """Build a worker.execution_completed event.

    Emitted when a worker container finishes executing a task.
    token_count_in/token_count_out mirror result.json fields from the worker agent.
    The error key is only included when error is not None.
    """
    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "job_id": job_id,
        "capability_id": capability_id,
        "container_id": container_id,
        "exit_code": exit_code,
        "success": success,
        "latency_ms": latency_ms,
        "token_count_in": token_count_in,
        "token_count_out": token_count_out,
    }
    if error is not None:
        payload["error"] = error
    return build_event(
        event_type="worker.execution_completed",
        job_id=job_id,
        capability_id=capability_id,
        source="orchestrator",
        payload=payload,
    )


# ---------- Phase 7A event builders ----------


def event_system_connectivity_hub_cloud(
    state: str,
    affected_routes: list[str],
    queued_jobs: int,
) -> dict[str, Any]:
    """Build a system.connectivity event for hub↔cloud link changes.

    state: "down" | "up" | "degraded"
    """
    return build_event(
        event_type="system.connectivity",
        job_id=None,
        source="orchestrator",
        durability="durable",
        payload={
            "link": "hub_cloud",
            "state": state,
            "affected_routes": affected_routes,
            "queued_jobs": queued_jobs,
        },
    )


def event_system_connectivity_device(
    link: str,
    state: str,
    queued_requests: int,
    duration_ms: int | None = None,
    replayed_requests: int = 0,
    dropped_requests: int = 0,
    replay_conflicts: int = 0,
) -> dict[str, Any]:
    """Build a system.connectivity event for device link changes.

    link: "watch_phone" | "phone_hub"
    state: "down" | "up" | "degraded"
    duration_ms: set on "up" events (outage duration)
    replayed_requests / replay_conflicts: set on "up" events
    dropped_requests: watch overflow count
    """
    return build_event(
        event_type="system.connectivity",
        job_id=None,
        source="phone_app",
        durability="durable",
        payload={
            "link": link,
            "state": state,
            "duration_ms": duration_ms,
            "queued_requests": queued_requests,
            "replayed_requests": replayed_requests,
            "dropped_requests": dropped_requests,
            "replay_conflicts": replay_conflicts,
        },
    )


def event_system_hub_switch(
    from_hub: str,
    to_hub: str,
    reason: str,
    pending_jobs_on_old_hub: int,
    suspend_command_delivered: bool,
) -> dict[str, Any]:
    """Build a system.hub_switch event when user switches active hub.

    reason: "manual" | "primary_failure"
    """
    return build_event(
        event_type="system.hub_switch",
        job_id=None,
        source="phone_app",
        durability="durable",
        payload={
            "from_hub": from_hub,
            "to_hub": to_hub,
            "reason": reason,
            "pending_jobs_on_old_hub": pending_jobs_on_old_hub,
            "suspend_command_delivered": suspend_command_delivered,
        },
    )


def event_wal_demoted_temporal_decay(
    capability_id: str,
    from_level: int,
    window_days: int,
    outcomes_in_window: int,
    min_required: int,
    last_outcome_timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a wal.demoted event triggered by temporal decay.

    to_level is always from_level - 1.
    """
    to_level = from_level - 1
    return build_event(
        event_type="wal.demoted",
        job_id=None,
        source="orchestrator",
        durability="durable",
        capability_id=capability_id,
        wal_level=to_level,
        payload={
            "trigger": "temporal_decay",
            "capability_id": capability_id,
            "from_level": from_level,
            "to_level": to_level,
            "window_days": window_days,
            "outcomes_in_window": outcomes_in_window,
            "min_required": min_required,
            "last_outcome_timestamp": last_outcome_timestamp,
        },
    )


def event_job_queued(
    job_id: str,
    reason: str,
    position: int,
    estimated_wait_ms: int | None = None,
) -> dict[str, Any]:
    """Build a job.queued event — best-effort durability.

    reason: "connectivity" | "rate_limit" | "capacity" | "recovery"
    """
    return build_event(
        event_type="job.queued",
        job_id=job_id,
        durability="best_effort",
        payload={
            "reason": reason,
            "position": position,
            "estimated_wait_ms": estimated_wait_ms,
        },
    )


def event_hub_state_change(
    from_state: str,
    to_state: str,
    reason: str,
    hostname: str,
) -> dict[str, Any]:
    """Build a system.hub_state_change event when hub operational state changes."""
    return build_event(
        event_type="system.hub_state_change",
        job_id=None,
        source="orchestrator",
        durability="durable",
        payload={
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "hostname": hostname,
        },
    )
