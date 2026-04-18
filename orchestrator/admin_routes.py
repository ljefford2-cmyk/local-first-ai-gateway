"""Admin endpoints for WAL lifecycle testing.

These are temporary testing scaffolding — they simulate inputs that will
eventually come from the phone/watch apps.  They inject state directly
rather than going through the full submit → classify → dispatch pipeline.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

import uuid_utils
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from events import event_system_model_change

# ---------------------------------------------------------------------------
# Dependencies — set by init() after lifespan initialization
# ---------------------------------------------------------------------------

_registry = None
_state_manager = None
_demotion_engine = None
_audit_client = None
_promotion_monitor = None
_startup_report = None
_audit_log_dir = "/var/drnt/audit"
_worker_lifecycle = None


def init(registry, state_manager, demotion_engine, audit_client, promotion_monitor,
         startup_report=None, audit_log_dir=None):
    """Called by main.py after lifespan initialization to inject dependencies."""
    global _registry, _state_manager, _demotion_engine, _audit_client, _promotion_monitor
    global _startup_report, _audit_log_dir
    _registry = registry
    _state_manager = state_manager
    _demotion_engine = demotion_engine
    _audit_client = audit_client
    _promotion_monitor = promotion_monitor
    if startup_report is not None:
        _startup_report = startup_report
    if audit_log_dir is not None:
        _audit_log_dir = audit_log_dir


def set_worker_lifecycle(worker_lifecycle) -> None:
    """TEST-ONLY wiring: attach the worker lifecycle for the syscall probe endpoint."""
    global _worker_lifecycle
    _worker_lifecycle = worker_lifecycle


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class RecordOutcomeRequest(BaseModel):
    capability_id: str
    disposition: str = Field(..., pattern="^(accepted|modified|rejected|resubmitted|auto_delivered)$")
    cost_usd: float | None = None
    source_event_id: str | None = None


class RecordOutcomeResponse(BaseModel):
    status: str
    capability_id: str
    disposition: str
    outcomes_count: int
    approval_score: float | None
    promotion_check: dict


class ModelChangeRequest(BaseModel):
    affected_capabilities: list[str] | None = None
    new_model_name: str = "manual-change"


class ModelChangeResponse(BaseModel):
    status: str
    demotions: list[dict]


class SimulateOverrideRequest(BaseModel):
    override_type: str = Field(..., pattern="^(cancel|redirect)$")
    governing_capability_id: str
    prior_failure_capability_id: str | None = None


class SimulateOverrideResponse(BaseModel):
    status: str
    demoted: bool
    detail: dict


class CapabilityStatusResponse(BaseModel):
    capabilities: list[dict]


class SyscallProbeResponse(BaseModel):
    """Result of the fixed seccomp enforcement probe (TEST-ONLY)."""
    status: str
    result_line: str
    container_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/admin", tags=["admin"])


def _guard():
    """Return a 503 JSONResponse if the system isn't initialized, else None."""
    if _state_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Orchestrator not initialized"},
        )
    return None


# ---------------------------------------------------------------------------
# POST /admin/record-outcome
# ---------------------------------------------------------------------------


@router.post("/record-outcome", response_model=RecordOutcomeResponse)
async def record_outcome(req: RecordOutcomeRequest):
    """Record a human review outcome for a capability.

    Simulates what the phone app's human.reviewed event will provide.
    Records the outcome in the capability's evaluable_outcomes ring buffer,
    sets first_job_date if not already set, then checks promotion readiness.
    """
    err = _guard()
    if err:
        return err

    if _registry.get(req.capability_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown capability: {req.capability_id}")

    source_event_id = req.source_event_id or str(uuid_utils.uuid7())

    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    outcome = {
        "source_event_id": source_event_id,
        "timestamp": timestamp,
        "disposition": req.disposition,
        "cost_usd": req.cost_usd,
    }

    _state_manager.record_outcome(req.capability_id, outcome)
    _state_manager.set_first_job_date(req.capability_id, timestamp)

    outcomes_count = len(_state_manager.get_outcomes(req.capability_id))
    approval_score = _state_manager.compute_approval_score(req.capability_id)

    promo_result = await _promotion_monitor.check_promotion(req.capability_id)
    promo_dict = {
        "ready": promo_result.ready,
        "capability_id": promo_result.capability_id,
        "current_level": promo_result.current_level,
        "recommended_level": promo_result.recommended_level,
        "reason": promo_result.reason,
        "notification_emitted": promo_result.notification_emitted,
        "source_event_id": promo_result.source_event_id,
    }

    return RecordOutcomeResponse(
        status="recorded",
        capability_id=req.capability_id,
        disposition=req.disposition,
        outcomes_count=outcomes_count,
        approval_score=approval_score,
        promotion_check=promo_dict,
    )


# ---------------------------------------------------------------------------
# POST /admin/model-change
# ---------------------------------------------------------------------------


@router.post("/model-change", response_model=ModelChangeResponse)
async def trigger_model_change(req: ModelChangeRequest):
    """Simulate a model change, demoting affected capabilities to WAL-0.

    If affected_capabilities is None, all non-meta capabilities are affected.
    Emits system.model_change event and triggers demotion engine.
    """
    err = _guard()
    if err:
        return err

    if req.affected_capabilities is None:
        affected = [cid for cid in _registry.get_all() if not cid.startswith("_")]
    else:
        affected = req.affected_capabilities

    for cid in affected:
        if _registry.get(cid) is None:
            raise HTTPException(status_code=400, detail=f"Unknown capability: {cid}")

    event = event_system_model_change(
        previous_model="unknown",
        new_model=req.new_model_name,
        affected_capabilities=affected,
    )
    await _audit_client.emit_durable(event)

    results = await _demotion_engine.handle_model_change(
        affected, source_event_id=event["source_event_id"],
    )

    demotions = [
        {
            "capability_id": r.capability_id,
            "from_level": r.from_level,
            "to_level": r.to_level,
            "trigger": r.trigger,
        }
        for r in results
    ]

    return ModelChangeResponse(status="processed", demotions=demotions)


# ---------------------------------------------------------------------------
# POST /admin/simulate-override
# ---------------------------------------------------------------------------


@router.post("/simulate-override", response_model=SimulateOverrideResponse)
async def simulate_override(req: SimulateOverrideRequest):
    """Simulate a cancel/redirect override on a WAL-1+ job.

    Tests the conditional demotion logic including sentinel failure exemption.
    """
    err = _guard()
    if err:
        return err

    if _registry.get(req.governing_capability_id) is None:
        raise HTTPException(status_code=404, detail=f"Unknown capability: {req.governing_capability_id}")

    source_event_id = str(uuid_utils.uuid7())

    job_context = {"prior_failure_capability_id": req.prior_failure_capability_id}

    result = await _demotion_engine.handle_override_demotion(
        override_type=req.override_type,
        governing_capability_id=req.governing_capability_id,
        job_context=job_context,
        source_event_id=source_event_id,
    )

    detail = {
        "demoted": result.demoted,
        "capability_id": result.capability_id,
        "from_level": result.from_level,
        "to_level": result.to_level,
        "trigger": result.trigger,
        "reason": result.reason,
        "source_event_id": result.source_event_id,
    }

    return SimulateOverrideResponse(
        status="processed",
        demoted=result.demoted,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# GET /admin/capabilities
# ---------------------------------------------------------------------------


@router.get("/capabilities", response_model=CapabilityStatusResponse)
async def get_capabilities():
    """View current capability state: effective WAL levels, counters, promotion readiness."""
    err = _guard()
    if err:
        return err

    summaries = []
    for cap_id, config in _registry.get_all().items():
        if cap_id.startswith("_"):
            continue

        state_entry = _state_manager.get(cap_id)
        if state_entry is None:
            continue

        score = _state_manager.compute_approval_score(cap_id)

        summaries.append({
            "capability_id": cap_id,
            "capability_name": config["capability_name"],
            "capability_type": config["capability_type"],
            "desired_wal_level": config["desired_wal_level"],
            "effective_wal_level": state_entry["effective_wal_level"],
            "max_wal": config["max_wal"],
            "status": state_entry["status"],
            "outcomes_count": len(state_entry["counters"]["evaluable_outcomes"]),
            "recent_failures_count": len(state_entry["counters"]["recent_failures"]),
            "approval_score": score,
            "first_job_date": state_entry["counters"]["first_job_date"],
            "last_reset": state_entry["counters"]["last_reset"],
            "last_incident": state_entry["counters"]["last_incident_source_event_id"],
        })

    return CapabilityStatusResponse(capabilities=summaries)


# ---------------------------------------------------------------------------
# GET /admin/startup-report
# ---------------------------------------------------------------------------


@router.get("/startup-report")
async def get_startup_report():
    """Return the startup validation report from the most recent hub start."""
    if _startup_report is None:
        return JSONResponse(
            status_code=404,
            content={"error": "no_startup_report", "detail": "No startup report available"},
        )
    checks = []
    for c in _startup_report.checks:
        checks.append({
            "check_name": c.check_name,
            "passed": c.passed,
            "severity": c.severity,
            "message": c.message,
            "details": c.details,
            "checked_at": c.checked_at,
        })
    return {
        "hub_start_permitted": _startup_report.hub_start_permitted,
        "checks": checks,
        "critical_failures": _startup_report.critical_failures,
        "warnings": _startup_report.warnings,
        "validated_at": _startup_report.validated_at,
    }


# ---------------------------------------------------------------------------
# GET /admin/audit-events
# ---------------------------------------------------------------------------


@router.get("/audit-events")
async def get_audit_events(
    event_type: str | None = None,
    limit: int = 100,
    job_id: str | None = None,
):
    """Read audit log events from the JSONL files on disk.

    Filters by event_type and/or job_id if provided. Returns the most
    recent events up to the limit.
    """
    import glob
    import json as _json
    import os

    events = []
    pattern = os.path.join(_audit_log_dir, "drnt-audit-*.jsonl")
    for filepath in sorted(glob.glob(pattern)):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if event_type and evt.get("event_type") != event_type:
                        continue
                    if job_id and evt.get("job_id") != job_id:
                        continue
                    events.append(evt)
        except OSError:
            continue
    return {"events": events[-limit:], "total": len(events)}


# ---------------------------------------------------------------------------
# POST /admin/e2e/syscall-probe  (TEST-ONLY — seccomp enforcement)
# ---------------------------------------------------------------------------


@router.post("/e2e/syscall-probe", response_model=SyscallProbeResponse)
async def e2e_syscall_probe():
    """TEST-ONLY. Run the fixed seccomp enforcement probe in a worker.

    The probe is fully hardcoded in the worker handler:
      target  = personality(0xFFFFFFFF)
      control = getpid()
    This endpoint accepts no body and exposes no parameters — no task_type
    override, no image override, no command, no security settings. It
    dispatches through the normal WorkerLifecycle path (manifest validation,
    blueprint, proxy registry enforcement) using capability route.local.
    """
    err = _guard()
    if err:
        return err

    if _worker_lifecycle is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "worker_lifecycle unavailable"},
        )

    probe_cap_id = "route.local"
    if _registry.get(probe_cap_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"probe capability {probe_cap_id} missing from registry",
        )

    import uuid_utils
    from models import Job

    probe_job = Job(
        job_id=str(uuid_utils.uuid7()),
        raw_input="__e2e_syscall_probe__",
        input_modality="text",
        device="watch",
        governing_capability_id=probe_cap_id,
    )

    ctx = await _worker_lifecycle.prepare_worker(probe_job)
    try:
        result = await _worker_lifecycle.execute_in_worker(
            ctx, prompt="", model="", task_type="syscall_probe",
        )
    finally:
        await _worker_lifecycle.teardown_worker(ctx)

    return SyscallProbeResponse(
        status="ok",
        result_line=result.get("response_text", ""),
        container_id=result.get("container_id"),
    )
