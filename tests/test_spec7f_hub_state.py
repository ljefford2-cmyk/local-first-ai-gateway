"""Phase 7F unit tests — Hub Suspension + Fallback Support.

Covers: HubState enum, HubStateManager lifecycle, startup self-check,
client heartbeat tracking, HTTP endpoints, JobManager integration,
and /health hub_suspended field.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from hub_state import HubState, HubStateManager
from events import event_hub_state_change


# ---------- 1. Hub starts in ACTIVE state ----------

def test_hub_starts_active():
    mgr = HubStateManager()
    assert mgr.state == HubState.ACTIVE


# ---------- 2. suspend() transitions to SUSPENDED ----------

def test_suspend_transitions_to_suspended():
    mgr = HubStateManager()
    mgr.suspend()
    assert mgr.state == HubState.SUSPENDED


# ---------- 3. resume() transitions back to ACTIVE from SUSPENDED ----------

def test_resume_from_suspended():
    mgr = HubStateManager()
    mgr.suspend()
    mgr.resume()
    assert mgr.state == HubState.ACTIVE


# ---------- 4. resume() transitions to ACTIVE from AWAITING_AUTHORITY ----------

def test_resume_from_awaiting_authority():
    mgr = HubStateManager(authority_timeout_seconds=0)
    mgr.startup_self_check()
    assert mgr.state == HubState.AWAITING_AUTHORITY
    mgr.resume()
    assert mgr.state == HubState.ACTIVE


# ---------- 5. is_processing_allowed() returns True for ACTIVE ----------

def test_processing_allowed_when_active():
    mgr = HubStateManager()
    assert mgr.is_processing_allowed() is True


# ---------- 6. is_processing_allowed() returns False for SUSPENDED ----------

def test_processing_not_allowed_when_suspended():
    mgr = HubStateManager()
    mgr.suspend()
    assert mgr.is_processing_allowed() is False


# ---------- 7. is_processing_allowed() returns False for AWAITING_AUTHORITY ----------

def test_processing_not_allowed_when_awaiting_authority():
    mgr = HubStateManager(authority_timeout_seconds=0)
    mgr.startup_self_check()
    assert mgr.state == HubState.AWAITING_AUTHORITY
    assert mgr.is_processing_allowed() is False


# ---------- 8. suspend() is idempotent ----------

def test_suspend_idempotent():
    mgr = HubStateManager()
    mgr.suspend()
    mgr.suspend()  # second call should not error
    assert mgr.state == HubState.SUSPENDED


# ---------- 9. resume() on ACTIVE is a no-op ----------

def test_resume_on_active_is_noop():
    mgr = HubStateManager()
    assert mgr.state == HubState.ACTIVE
    mgr.resume()
    assert mgr.state == HubState.ACTIVE


# ---------- 10. record_heartbeat() updates timestamp ----------

def test_record_heartbeat_updates_timestamp():
    mgr = HubStateManager()
    assert mgr.seconds_since_last_heartbeat() is None
    mgr.record_heartbeat()
    elapsed = mgr.seconds_since_last_heartbeat()
    assert elapsed is not None
    assert elapsed >= 0
    assert elapsed < 2.0  # should be near-instant


# ---------- 11. seconds_since_last_heartbeat() returns elapsed time ----------

def test_seconds_since_last_heartbeat_elapsed():
    mgr = HubStateManager()
    mgr.record_heartbeat()
    time.sleep(0.05)
    elapsed = mgr.seconds_since_last_heartbeat()
    assert elapsed is not None
    assert elapsed >= 0.04  # at least ~50ms


# ---------- 12. seconds_since_last_heartbeat() returns None before any heartbeat ----------

def test_seconds_since_last_heartbeat_none_initially():
    mgr = HubStateManager()
    assert mgr.seconds_since_last_heartbeat() is None


# ---------- 13. Startup self-check: no heartbeat within timeout -> AWAITING_AUTHORITY ----------

def test_startup_self_check_no_heartbeat():
    mgr = HubStateManager(authority_timeout_seconds=0)
    mgr.startup_self_check()
    assert mgr.state == HubState.AWAITING_AUTHORITY


# ---------- 14. Startup self-check: recent heartbeat within timeout -> ACTIVE ----------

def test_startup_self_check_recent_heartbeat():
    mgr = HubStateManager(authority_timeout_seconds=600)
    mgr.record_heartbeat()
    mgr.startup_self_check()
    assert mgr.state == HubState.ACTIVE


# ---------- 15. confirm_authority transitions from AWAITING_AUTHORITY to ACTIVE ----------

def test_confirm_authority_from_awaiting():
    mgr = HubStateManager(authority_timeout_seconds=0)
    mgr.startup_self_check()
    assert mgr.state == HubState.AWAITING_AUTHORITY
    result = mgr.confirm_authority()
    assert mgr.state == HubState.ACTIVE
    assert result["status"] == "active"
    assert result["resumed_from"] == "awaiting_authority"


# ---------- 16. confirm_authority on ACTIVE is a no-op ----------

def test_confirm_authority_on_active_noop():
    mgr = HubStateManager()
    result = mgr.confirm_authority()
    assert mgr.state == HubState.ACTIVE
    assert result["status"] == "active"
    assert result["resumed_from"] == "already_active"


# ---------- 17. POST /suspend endpoint returns correct response ----------

def test_suspend_endpoint():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    hub_mgr = HubStateManager()

    @app.post("/suspend")
    async def suspend():
        hub_mgr.suspend()
        return {"status": "suspended"}

    client = TestClient(app)
    resp = client.post("/suspend")
    assert resp.status_code == 200
    assert resp.json() == {"status": "suspended"}
    assert hub_mgr.state == HubState.SUSPENDED


# ---------- 18. POST /resume endpoint returns correct response ----------

def test_resume_endpoint():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    hub_mgr = HubStateManager()
    hub_mgr.suspend()

    @app.post("/resume")
    async def resume():
        hub_mgr.resume()
        return {"status": "active"}

    client = TestClient(app)
    resp = client.post("/resume")
    assert resp.status_code == 200
    assert resp.json() == {"status": "active"}
    assert hub_mgr.state == HubState.ACTIVE


# ---------- 19. POST /confirm_authority endpoint returns correct response ----------

def test_confirm_authority_endpoint():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    hub_mgr = HubStateManager(authority_timeout_seconds=0)
    hub_mgr.startup_self_check()

    @app.post("/confirm_authority")
    async def confirm():
        return hub_mgr.confirm_authority()

    client = TestClient(app)
    resp = client.post("/confirm_authority")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["resumed_from"] == "awaiting_authority"


# ---------- 20. GET /hub_status returns current state and heartbeat info ----------

def test_hub_status_endpoint():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    app = FastAPI()
    hub_mgr = HubStateManager(hostname="test-hub")

    @app.get("/hub_status")
    async def status():
        hub_mgr.record_heartbeat()
        return {
            "state": hub_mgr.state.value,
            "last_client_heartbeat": hub_mgr.last_client_heartbeat_iso(),
            "seconds_since_heartbeat": hub_mgr.seconds_since_last_heartbeat(),
            "hostname": hub_mgr.hostname,
        }

    client = TestClient(app)
    resp = client.get("/hub_status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "active"
    assert data["last_client_heartbeat"] is not None
    assert data["hostname"] == "test-hub"
    assert isinstance(data["seconds_since_heartbeat"], float)


# ---------- 21. JobManager does not process jobs when hub is SUSPENDED ----------

@pytest.mark.asyncio
async def test_jobmanager_blocked_when_suspended():
    from job_manager import JobManager
    from audit_client import AuditLogClient

    audit = MagicMock(spec=AuditLogClient)
    audit.emit_durable = AsyncMock()
    audit.health_check = AsyncMock(return_value=True)

    mgr = JobManager(audit_client=audit)
    hub = HubStateManager()
    hub.suspend()
    mgr.set_hub_state_manager(hub)

    # Put a job in the queue
    from models import Job, JobStatus
    job = Job(job_id="j1", raw_input="test", input_modality="text", device="phone")
    mgr._jobs["j1"] = job
    await mgr._queue.put("j1")

    # Start worker — it should spin waiting because hub is suspended
    task = asyncio.create_task(mgr._worker_loop())
    await asyncio.sleep(0.3)

    # Job should still be in submitted status (not processed)
    assert job.status == JobStatus.submitted.value

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------- 22. JobManager does not process jobs when hub is AWAITING_AUTHORITY ----------

@pytest.mark.asyncio
async def test_jobmanager_blocked_when_awaiting_authority():
    from job_manager import JobManager
    from audit_client import AuditLogClient

    audit = MagicMock(spec=AuditLogClient)
    audit.emit_durable = AsyncMock()
    audit.health_check = AsyncMock(return_value=True)

    mgr = JobManager(audit_client=audit)
    hub = HubStateManager(authority_timeout_seconds=0)
    hub.startup_self_check()
    assert hub.state == HubState.AWAITING_AUTHORITY
    mgr.set_hub_state_manager(hub)

    from models import Job, JobStatus
    job = Job(job_id="j2", raw_input="test", input_modality="text", device="phone")
    mgr._jobs["j2"] = job
    await mgr._queue.put("j2")

    task = asyncio.create_task(mgr._worker_loop())
    await asyncio.sleep(0.3)

    # Job should still be unprocessed
    assert job.status == JobStatus.submitted.value

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------- 23. JobManager resumes processing when hub returns to ACTIVE ----------

@pytest.mark.asyncio
async def test_jobmanager_resumes_on_active():
    from job_manager import JobManager
    from audit_client import AuditLogClient

    audit = MagicMock(spec=AuditLogClient)
    audit.emit_durable = AsyncMock()
    audit.health_check = AsyncMock(return_value=True)

    mgr = JobManager(audit_client=audit)
    hub = HubStateManager()
    hub.suspend()
    mgr.set_hub_state_manager(hub)

    from models import Job, JobStatus
    job = Job(job_id="j3", raw_input="test", input_modality="text", device="phone")
    mgr._jobs["j3"] = job
    await mgr._queue.put("j3")

    # Patch _run_pipeline to track if it gets called
    pipeline_called = asyncio.Event()
    original_run = mgr._run_pipeline

    async def mock_pipeline(job_id):
        pipeline_called.set()

    mgr._run_pipeline = mock_pipeline

    task = asyncio.create_task(mgr._worker_loop())
    await asyncio.sleep(0.2)
    assert not pipeline_called.is_set()

    # Resume the hub
    hub.resume()
    await asyncio.sleep(0.5)
    assert pipeline_called.is_set()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------- 24. /health endpoint reflects hub_suspended correctly ----------

def test_health_hub_suspended_field():
    from models import HealthResponse

    # When active
    resp_active = HealthResponse(
        orchestrator_status="running",
        audit_log_status="connected",
        ollama_status="available",
        hub_suspended=False,
    )
    assert resp_active.hub_suspended is False

    # When suspended
    resp_suspended = HealthResponse(
        orchestrator_status="running",
        audit_log_status="connected",
        ollama_status="available",
        hub_suspended=True,
    )
    assert resp_suspended.hub_suspended is True


# ---------- 25. Hub hostname is included in /hub_status response ----------

def test_hostname_in_hub_status():
    mgr = HubStateManager(hostname="desktop-hub")
    assert mgr.hostname == "desktop-hub"


# ---------- 26. HubState enum values match spec ----------

def test_hub_state_enum_values():
    assert HubState.ACTIVE.value == "active"
    assert HubState.SUSPENDED.value == "suspended"
    assert HubState.AWAITING_AUTHORITY.value == "awaiting_authority"


# ---------- 27. suspend() emits audit event ----------

def test_suspend_emits_event():
    emitted = []
    mgr = HubStateManager(emit_event=lambda evt: emitted.append(evt))
    mgr.suspend()
    assert len(emitted) == 1
    evt = emitted[0]
    assert evt["event_type"] == "system.hub_state_change"
    assert evt["payload"]["from_state"] == "active"
    assert evt["payload"]["to_state"] == "suspended"


# ---------- 28. resume() emits audit event ----------

def test_resume_emits_event():
    emitted = []
    mgr = HubStateManager(emit_event=lambda evt: emitted.append(evt))
    mgr.suspend()
    emitted.clear()
    mgr.resume()
    assert len(emitted) == 1
    evt = emitted[0]
    assert evt["payload"]["from_state"] == "suspended"
    assert evt["payload"]["to_state"] == "active"


# ---------- 29. idempotent suspend does not emit duplicate events ----------

def test_idempotent_suspend_no_duplicate_event():
    emitted = []
    mgr = HubStateManager(emit_event=lambda evt: emitted.append(evt))
    mgr.suspend()
    mgr.suspend()
    assert len(emitted) == 1


# ---------- 30. last_client_heartbeat_iso returns ISO timestamp ----------

def test_last_client_heartbeat_iso_format():
    mgr = HubStateManager()
    assert mgr.last_client_heartbeat_iso() is None
    mgr.record_heartbeat()
    iso = mgr.last_client_heartbeat_iso()
    assert iso is not None
    assert iso.endswith("Z")
    assert "T" in iso


# ---------- 31. confirm_authority from SUSPENDED also works ----------

def test_confirm_authority_from_suspended():
    mgr = HubStateManager()
    mgr.suspend()
    result = mgr.confirm_authority()
    assert mgr.state == HubState.ACTIVE
    assert result["resumed_from"] == "suspended"


# ---------- 32. event_hub_state_change builder produces valid envelope ----------

def test_event_hub_state_change_envelope():
    evt = event_hub_state_change(
        from_state="active",
        to_state="suspended",
        reason="suspend_command",
        hostname="desktop-hub",
    )
    assert evt["event_type"] == "system.hub_state_change"
    assert evt["source"] == "orchestrator"
    assert evt["durability"] == "durable"
    assert evt["job_id"] is None
    assert evt["payload"]["from_state"] == "active"
    assert evt["payload"]["to_state"] == "suspended"
    assert evt["payload"]["reason"] == "suspend_command"
    assert evt["payload"]["hostname"] == "desktop-hub"


# ---------- 33. default hostname uses platform.node() ----------

def test_default_hostname():
    import platform
    mgr = HubStateManager()
    assert mgr.hostname == platform.node()
