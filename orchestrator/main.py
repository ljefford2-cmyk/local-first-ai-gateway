"""DRNT Orchestrator — Phase 4.

FastAPI application exposing the job lifecycle HTTP API.
Communicates with the Phase 1 audit log writer over Unix domain socket.
Routes requests through Ollama classifier and egress gateway.
Startup validates capabilities, reconciles WAL levels, and checks model versions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from audit_client import AuditLogClient
from persistence import init_db, close_db
from capability_registry import CapabilityRegistry
from classifier import check_ollama_health
from capability_state import CapabilityStateManager
from context_packager import ContextPackager, SensitivityConfig
from demotion_engine import DemotionEngine
from hub_state import HubState, HubStateManager
from job_manager import JobManager
from models import (
    HealthResponse,
    JobStatus,
    JobStatusResponse,
    JobSubmitRequest,
    JobSubmitResponse,
    ReviewRequest,
)
from pydantic import BaseModel
from typing import Optional


class OverrideRequest(BaseModel):
    override_type: str  # cancel | redirect | modify | escalate
    target: str         # "routing" for cancel/redirect/escalate, "response" for modify
    detail: str         # e.g. "route.cloud.openai" for redirect, modification summary for modify
    device: str         # "watch" | "phone"
    modified_result: Optional[str] = None  # required for modify overrides
from permission_checker import PermissionChecker

logging.basicConfig(
    level=os.environ.get("DRNT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SOCKET_PATH = os.environ.get(
    "DRNT_AUDIT_SOCKET", "/var/drnt/sockets/audit.sock"
)
SENSITIVITY_CONFIG_PATH = os.environ.get(
    "DRNT_SENSITIVITY_CONFIG", "/var/drnt/config/sensitivity.json"
)
CONTEXT_STORE_DIR = os.environ.get(
    "DRNT_CONTEXT_STORE", "/var/drnt/context"
)
CAPABILITIES_CONFIG_PATH = os.environ.get(
    "DRNT_CAPABILITIES_CONFIG", "/var/drnt/config/capabilities.json"
)
CAPABILITIES_STATE_PATH = os.environ.get(
    "DRNT_CAPABILITIES_STATE", "/var/drnt/state/capabilities.state.json"
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")

audit_client = AuditLogClient(socket_path=SOCKET_PATH)

# Cached Ollama health probe (30-second TTL, 2-second timeout)
_ollama_cache: dict[str, object] = {"status": "unavailable", "ts": 0.0}
OLLAMA_CACHE_TTL = 30.0
OLLAMA_PROBE_TIMEOUT = 2.0


async def _cached_ollama_status() -> str:
    """Return 'available' or 'unavailable', cached for 30 seconds."""
    now = time.monotonic()
    if now - _ollama_cache["ts"] < OLLAMA_CACHE_TTL:
        return _ollama_cache["status"]
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            status = "available" if resp.status_code == 200 else "unavailable"
    except Exception:
        status = "unavailable"
    _ollama_cache["status"] = status
    _ollama_cache["ts"] = now
    return status


# Initialized in lifespan
job_manager: JobManager | None = None
_registry: CapabilityRegistry | None = None
_state_manager: CapabilityStateManager | None = None
_permission_checker: PermissionChecker | None = None
_demotion_engine: DemotionEngine | None = None
_promotion_monitor = None
_connectivity_monitor = None
_hub_state_manager: HubStateManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global job_manager, _registry, _state_manager, _permission_checker, _demotion_engine, _promotion_monitor, _connectivity_monitor, _hub_state_manager

    logger.info("Orchestrator starting, socket=%s", SOCKET_PATH)

    # Phase 6D: Run startup validation (Items 8-10) before anything else
    from startup_validator import HubConfig, StartupValidator, StartupValidationError
    from events import event_hub_startup_validated, event_hub_startup_blocked

    hub_config = HubConfig(
        worker_proxy_url=os.environ.get("DRNT_WORKER_PROXY_URL", "http://worker-proxy:9100"),
        worker_base_image=os.environ.get("DRNT_WORKER_IMAGE", "drnt-worker:latest"),
        sandbox_base_dir=os.environ.get("DRNT_SANDBOX_DIR", "/var/drnt/workers"),
        seccomp_profile_path=os.environ.get("DRNT_SECCOMP_PROFILE", "/var/drnt/config/seccomp-default.json"),
        egress_config_path=os.environ.get("DRNT_EGRESS_CONFIG", "/var/drnt/config/egress.json"),
        ollama_url=OLLAMA_URL,
        audit_log_dir=os.environ.get("DRNT_AUDIT_DIR", "/var/drnt/audit"),
        egress_state_dir=os.environ.get("DRNT_EGRESS_STATE_DIR", "/var/drnt/state/egress"),
    )

    validator = StartupValidator(hub_config)
    validation_report = await validator.validate_all()

    if not validation_report.hub_start_permitted:
        # Log the blocked event and refuse to start
        checks_passed = sum(1 for c in validation_report.checks if c.passed)
        checks_failed = sum(1 for c in validation_report.checks if not c.passed)
        blocked_event = event_hub_startup_blocked(
            blocking_checks=validation_report.critical_failures,
            message=f"Hub startup blocked: {', '.join(validation_report.critical_failures)} failed validation",
        )
        try:
            await audit_client.emit_durable(blocked_event)
        except Exception:
            logger.error("Could not emit startup_blocked event to audit log")
        for name in validation_report.critical_failures:
            logger.error("Startup validation CRITICAL failure: %s", name)
        raise StartupValidationError(validation_report)

    # Emit startup_validated event
    checks_passed = sum(1 for c in validation_report.checks if c.passed)
    checks_failed = sum(1 for c in validation_report.checks if not c.passed)
    validated_event = event_hub_startup_validated(
        hub_start_permitted=True,
        checks_passed=checks_passed,
        checks_failed=checks_failed,
        critical_failures=[],
        warnings=validation_report.warnings,
    )
    try:
        await audit_client.emit_durable(validated_event)
    except Exception:
        logger.warning("Could not emit startup_validated event to audit log")

    for w in validation_report.warnings:
        logger.warning("Startup validation warning: %s", w)

    logger.info("Startup validation passed (%d checks, %d warnings)", checks_passed, len(validation_report.warnings))

    # Initialize capability system
    _registry = CapabilityRegistry(config_path=CAPABILITIES_CONFIG_PATH)
    _state_manager = CapabilityStateManager(state_path=CAPABILITIES_STATE_PATH)

    # Run startup sequence (validation + reconciliation)
    from startup import run as startup_run

    startup_result = await startup_run(
        registry=_registry,
        state_manager=_state_manager,
        audit_client=audit_client,
        sensitivity_config_path=SENSITIVITY_CONFIG_PATH,
        ollama_url=OLLAMA_URL,
    )

    if not startup_result.success:
        for err in startup_result.errors:
            logger.error("Startup validation failed: %s", err)
        raise SystemExit("Startup validation failed — refusing to accept requests")

    logger.info(
        "Startup complete: config_hash=%s, state_hash=%s, ollama=%s, reconciliation_events=%d",
        startup_result.config_hash[:16] if startup_result.config_hash else "none",
        startup_result.state_hash[:16] if startup_result.state_hash else "none",
        startup_result.ollama_model or "unknown",
        startup_result.reconciliation_events,
    )

    # Build permission checker, demotion engine, and promotion monitor
    _permission_checker = PermissionChecker(_registry, _state_manager, audit_client)
    _demotion_engine = DemotionEngine(_registry, _state_manager, audit_client)

    from promotion_monitor import PromotionMonitor
    _promotion_monitor = PromotionMonitor(_registry, _state_manager, audit_client)

    import admin_routes
    admin_routes.init(
        _registry, _state_manager, _demotion_engine, audit_client, _promotion_monitor,
        startup_report=validation_report,
        audit_log_dir=os.environ.get("DRNT_AUDIT_DIR", "/var/drnt/audit"),
    )

    # Initialize worker lifecycle (Spec 6E)
    from worker_lifecycle import WorkerLifecycle, EgressPolicyStore
    from manifest_validator import ManifestValidator, build_egress_endpoints_from_routes
    from blueprint_engine import BlueprintEngine
    from egress_rate_limiter import EgressRateLimiter
    import json as _json

    _egress_policy_store = EgressPolicyStore()
    _egress_endpoints: dict[str, list[str]] = {}
    _egress_config_path = os.environ.get("DRNT_EGRESS_CONFIG", "/var/drnt/config/egress.json")
    try:
        with open(_egress_config_path, "r", encoding="utf-8") as f:
            _egress_data = _json.load(f)
        _egress_policy_store.load_from_routes(_egress_data.get("routes", []))
        _egress_endpoints = build_egress_endpoints_from_routes(_egress_data.get("routes", []))
        logger.info("Egress policies loaded for worker lifecycle")
    except Exception:
        logger.warning("Could not load egress routes for worker lifecycle", exc_info=True)

    _manifest_validator = ManifestValidator(
        _registry, _state_manager, egress_endpoints=_egress_endpoints,
    )
    _blueprint_engine = BlueprintEngine(
        base_dir=os.environ.get("DRNT_SANDBOX_DIR", "/var/drnt/workers"),
        image=os.environ.get("DRNT_WORKER_IMAGE", "drnt-worker:latest"),
    )
    _rate_limiter = EgressRateLimiter()

    # Priority #5: Initialize worker executor for sandboxed container execution
    from worker_executor import WorkerExecutor

    _worker_executor = None
    try:
        _we = WorkerExecutor(
            sandbox_base_dir=os.environ.get("DRNT_SANDBOX_DIR", "/var/drnt/workers"),
            ollama_url=OLLAMA_URL,
        )
        if not _we.check_docker_available():
            logger.warning("Worker proxy not available — worker executor disabled (direct dispatch fallback)")
        else:
            _worker_image = os.environ.get("DRNT_WORKER_IMAGE", "drnt-worker:latest")
            if not _we.check_image_exists(_worker_image):
                logger.warning(
                    "Worker image %s not found — build with: docker compose build worker",
                    _worker_image,
                )
            else:
                _worker_executor = _we
    except Exception:
        logger.warning("Worker executor init failed — falling back to direct dispatch", exc_info=True)

    _worker_lifecycle = WorkerLifecycle(
        manifest_validator=_manifest_validator,
        blueprint_engine=_blueprint_engine,
        capability_registry=_registry,
        egress_policy_store=_egress_policy_store,
        audit_client=audit_client,
        rate_limiter=_rate_limiter,
        state_manager=_state_manager,
        worker_executor=_worker_executor,
        seccomp_profile_content=hub_config.seccomp_profile_content,
    )
    admin_routes.set_worker_lifecycle(_worker_lifecycle)
    logger.info(
        "Worker lifecycle initialized (executor=%s)",
        "active" if _worker_executor is not None else "none — direct dispatch fallback",
    )

    # Phase 7C: Initialize connectivity monitor
    from connectivity_monitor import ConnectivityMonitor
    _egress_routes_raw = _egress_data.get("routes", []) if "_egress_data" in dir() else []
    _connectivity_monitor = ConnectivityMonitor(
        routes=_egress_routes_raw,
        emit_event=lambda evt: asyncio.ensure_future(audit_client.emit_durable(evt)),
        get_queued_count=lambda: job_manager.queued_count if job_manager and hasattr(job_manager, "queued_count") else 0,
    )
    logger.info("Connectivity monitor initialized (%d routes)", len(_egress_routes_raw))

    # Initialize context packager
    _packager: ContextPackager | None = None
    try:
        _sensitivity_config = SensitivityConfig(SENSITIVITY_CONFIG_PATH)
        _packager = ContextPackager(
            config=_sensitivity_config,
            audit_client=audit_client,
            context_store_dir=CONTEXT_STORE_DIR,
        )
        logger.info("Context packager ready")
    except Exception:
        logger.error(
            "Context packager failed to initialize — cloud dispatch will be blocked",
            exc_info=True,
        )

    # Initialize SQLite persistence
    try:
        await init_db()
        logger.info("SQLite persistence initialized")
    except Exception:
        logger.warning("SQLite persistence unavailable — using in-memory stores", exc_info=True)

    # Create job manager with full capability system
    job_manager = JobManager(
        audit_client=audit_client,
        context_packager=_packager,
        permission_checker=_permission_checker,
        demotion_engine=_demotion_engine,
        worker_lifecycle=_worker_lifecycle,
        connectivity_monitor=_connectivity_monitor,
    )

    # Phase 7F: Initialize hub state manager
    _hub_state_manager = HubStateManager(
        emit_event=lambda evt: asyncio.ensure_future(audit_client.emit_durable(evt)),
    )
    job_manager.set_hub_state_manager(_hub_state_manager)
    logger.info("Hub state manager initialized (hostname=%s)", _hub_state_manager.hostname)

    # Warm up Ollama
    try:
        from classifier import classify

        logger.info("Warming up Ollama classifier model...")
        await classify("warmup")
        logger.info("Ollama warm-up complete")
    except Exception:
        logger.warning("Ollama warm-up failed — first request may be slow")

    # Phase 7D: Stale job recovery pass — scans persisted non-terminal jobs
    from stale_recovery import StaleJobRecovery
    from events import event_system_startup as _build_recovery_startup

    _recovery = StaleJobRecovery(audit_client)
    _recovery_report = await _recovery.run_recovery(job_manager._jobs, job_manager)
    logger.info(
        "Recovery pass: scanned=%d recovered=%d skipped=%d failed=%d",
        _recovery_report.jobs_scanned,
        _recovery_report.jobs_recovered,
        _recovery_report.jobs_skipped,
        _recovery_report.jobs_failed,
    )

    # Persist any recovery-modified jobs back to SQLite
    for _rjob in list(job_manager._jobs.values()):
        job_manager._persist_job(_rjob)

    _recovery_event = _build_recovery_startup(
        recovery_summary={
            "jobs_scanned": _recovery_report.jobs_scanned,
            "jobs_recovered": _recovery_report.jobs_recovered,
            "jobs_skipped": _recovery_report.jobs_skipped,
            "jobs_failed": _recovery_report.jobs_failed,
        },
    )
    await audit_client.emit_durable(_recovery_event)

    # Phase 7F: Startup self-check for hub authority
    _hub_state_manager.startup_self_check()

    await job_manager.start()

    # Phase 7C: Start background connectivity probes
    if _connectivity_monitor is not None:
        await _connectivity_monitor.start_monitoring(interval_seconds=30)

    logger.info("Orchestrator ready — accepting requests")

    yield

    # Phase 7C: Stop connectivity monitor
    if _connectivity_monitor is not None:
        await _connectivity_monitor.stop_monitoring()

    await job_manager.stop()
    await close_db()
    logger.info("Orchestrator stopped")


app = FastAPI(title="DRNT Orchestrator", version="0.4.0", lifespan=lifespan)

from admin_routes import router as admin_router
app.include_router(admin_router)


# ---------- Endpoints ----------


@app.post("/jobs", status_code=202, response_model=JobSubmitResponse)
async def submit_job(req: JobSubmitRequest):
    if job_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Orchestrator not initialized"},
        )
    try:
        job = await job_manager.submit_job(
            raw_input=req.raw_input,
            input_modality=req.input_modality.value,
            device=req.device.value,
            idempotency_key=req.idempotency_key,
        )
    except TimeoutError:
        return JSONResponse(
            status_code=503,
            content={
                "error": "audit_log_unavailable",
                "detail": "Cannot accept jobs without audit logging.",
            },
        )
    except Exception as exc:
        logger.exception("Job submission failed")
        return JSONResponse(
            status_code=503,
            content={
                "error": "audit_log_unavailable",
                "detail": "Cannot accept jobs without audit logging.",
            },
        )

    return JobSubmitResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
    )


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: str):
    if job_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Orchestrator not initialized"},
        )
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    proposal = job_manager.get_proposal(job_id)

    return JobStatusResponse(
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


@app.get("/health", response_model=HealthResponse)
async def health_check():
    if job_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Orchestrator not initialized"},
        )
    audit_ok = await audit_client.health_check()
    ollama_status = await _cached_ollama_status()

    cloud_probe_ts = None
    if _connectivity_monitor is not None:
        ts = _connectivity_monitor.last_successful_cloud_probe_timestamp
        if ts is not None:
            cloud_probe_ts = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    hub_suspended = (
        _hub_state_manager.state != HubState.ACTIVE
        if _hub_state_manager is not None
        else False
    )

    return HealthResponse(
        orchestrator_status="running",
        audit_log_status="connected" if audit_ok else "unavailable",
        ollama_status=ollama_status,
        last_successful_cloud_probe_timestamp=cloud_probe_ts,
        hub_suspended=hub_suspended,
    )


# ---------- Phase 7F: Hub State Endpoints ----------


@app.post("/suspend")
async def suspend_hub():
    if _hub_state_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Hub state manager not initialized"},
        )
    _hub_state_manager.suspend()
    return {"status": "suspended"}


@app.post("/resume")
async def resume_hub():
    if _hub_state_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Hub state manager not initialized"},
        )
    _hub_state_manager.resume()
    return {"status": "active"}


@app.post("/confirm_authority")
async def confirm_authority():
    if _hub_state_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Hub state manager not initialized"},
        )
    result = _hub_state_manager.confirm_authority()
    return result


@app.get("/hub_status")
async def hub_status():
    if _hub_state_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Hub state manager not initialized"},
        )
    _hub_state_manager.record_heartbeat()
    return {
        "state": _hub_state_manager.state.value,
        "last_client_heartbeat": _hub_state_manager.last_client_heartbeat_iso(),
        "seconds_since_heartbeat": _hub_state_manager.seconds_since_last_heartbeat(),
        "hostname": _hub_state_manager.hostname,
    }


@app.post("/jobs/{job_id}/review")
async def review_job(job_id: str, req: ReviewRequest):
    if job_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Orchestrator not initialized"},
        )
    status_code, body = await job_manager.review_job(
        job_id=job_id,
        decision=req.decision.value,
        result_id=req.result_id,
        response_hash=req.response_hash,
        decision_idempotency_key=req.decision_idempotency_key,
        modified_result=req.modified_result,
    )
    return JSONResponse(status_code=status_code, content=body)


@app.post("/jobs/{job_id}/override")
async def override_job(job_id: str, req: OverrideRequest):
    if job_manager is None:
        return JSONResponse(
            status_code=503,
            content={"error": "not_ready", "detail": "Orchestrator not initialized"},
        )
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    result = await job_manager.override_job(
        job_id=job_id,
        override_type=req.override_type,
        target=req.target,
        detail=req.detail,
        device=req.device,
        modified_result=req.modified_result,
    )
    return result
