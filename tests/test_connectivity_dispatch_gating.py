"""Tests for ConnectivityMonitor dispatch gating in JobManager._run_pipeline().

Verifies that cloud jobs are blocked when the circuit breaker is OPEN,
and proceed normally when CLOSED, HALF_OPEN, or no monitor is configured.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from classifier import ClassificationResult
from job_manager import JobManager
from models import JobStatus
from test_helpers import MockAuditClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cloud_classification() -> ClassificationResult:
    return ClassificationResult(
        category="analysis",
        local_capable=False,
        routing="cloud",
        recommended_model="claude",
        confidence=0.9,
        capability_id="route.cloud.claude",
        candidate_models=["claude-sonnet-4-20250514"],
        route_id="claude-sonnet-default",
    )


def _make_connectivity_monitor(*, available: bool) -> MagicMock:
    """Return a mock ConnectivityMonitor with is_route_available returning *available*."""
    monitor = MagicMock()
    monitor.is_route_available.return_value = available
    return monitor


def _make_context_packager() -> MagicMock:
    """Return a mock ContextPackager whose package() is an AsyncMock."""
    packager = MagicMock()
    result = MagicMock()
    result.context_package_id = "ctx-test-001"
    result.assembled_payload_hash = "abc123"
    result.assembled_payload = "Packaged prompt"
    packager.package = AsyncMock(return_value=result)
    return packager


def _make_egress_client() -> AsyncMock:
    """Return a mock httpx.AsyncClient for egress gateway calls."""
    resp = MagicMock()
    resp.json.return_value = {
        "status": "ok",
        "response_text": "Cloud response.",
        "latency_ms": 200,
        "token_count_in": 10,
        "token_count_out": 20,
        "cost_estimate_usd": 0.005,
        "result_id": "result-001",
        "response_hash": "hash123",
        "finish_reason": "stop",
    }
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


async def _submit_and_run(jm: JobManager) -> str:
    """Submit a job and run the pipeline directly. Returns job_id."""
    job = await jm.submit_job(
        raw_input="test prompt",
        input_modality="text",
        device="phone",
    )
    await jm._run_pipeline(job.job_id)
    return job.job_id


# ---------------------------------------------------------------------------
# 1. _run_pipeline succeeds normally when no ConnectivityMonitor is set (None)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_succeeds_without_monitor():
    audit = MockAuditClient()
    packager = _make_context_packager()
    jm = JobManager(audit_client=audit, context_packager=packager, connectivity_monitor=None)

    egress_client = _make_egress_client()
    with (
        patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())),
        patch("job_manager.httpx.AsyncClient", return_value=egress_client),
    ):
        job_id = await _submit_and_run(jm)

    job = jm.get_job(job_id)
    assert job.status == JobStatus.delivered.value


# ---------------------------------------------------------------------------
# 2. _run_pipeline succeeds when ConnectivityMonitor reports route CLOSED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_succeeds_route_closed():
    audit = MockAuditClient()
    packager = _make_context_packager()
    monitor = _make_connectivity_monitor(available=True)
    jm = JobManager(
        audit_client=audit, context_packager=packager, connectivity_monitor=monitor,
    )

    egress_client = _make_egress_client()
    with (
        patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())),
        patch("job_manager.httpx.AsyncClient", return_value=egress_client),
    ):
        job_id = await _submit_and_run(jm)

    job = jm.get_job(job_id)
    assert job.status == JobStatus.delivered.value
    monitor.is_route_available.assert_called_with("claude-sonnet-default")


# ---------------------------------------------------------------------------
# 3. _run_pipeline succeeds when ConnectivityMonitor reports route HALF_OPEN
#    (is_route_available returns True for HALF_OPEN)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_succeeds_route_half_open():
    audit = MockAuditClient()
    packager = _make_context_packager()
    # HALF_OPEN -> is_route_available returns True
    monitor = _make_connectivity_monitor(available=True)
    jm = JobManager(
        audit_client=audit, context_packager=packager, connectivity_monitor=monitor,
    )

    egress_client = _make_egress_client()
    with (
        patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())),
        patch("job_manager.httpx.AsyncClient", return_value=egress_client),
    ):
        job_id = await _submit_and_run(jm)

    job = jm.get_job(job_id)
    assert job.status == JobStatus.delivered.value


# ---------------------------------------------------------------------------
# 4. _run_pipeline fails job when ConnectivityMonitor reports route OPEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_fails_route_open():
    audit = MockAuditClient()
    packager = _make_context_packager()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=packager, connectivity_monitor=monitor,
    )

    with patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())):
        job_id = await _submit_and_run(jm)

    job = jm.get_job(job_id)
    assert job.status == JobStatus.failed.value


# ---------------------------------------------------------------------------
# 5. Failed job has error string containing "route_unavailable"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_job_error_contains_route_unavailable():
    audit = MockAuditClient()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=_make_context_packager(),
        connectivity_monitor=monitor,
    )

    with patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())):
        job_id = await _submit_and_run(jm)

    job = jm.get_job(job_id)
    assert "route_unavailable" in job.error


# ---------------------------------------------------------------------------
# 6. Failed job has error string containing the route_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_job_error_contains_route_id():
    audit = MockAuditClient()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=_make_context_packager(),
        connectivity_monitor=monitor,
    )

    with patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())):
        job_id = await _submit_and_run(jm)

    job = jm.get_job(job_id)
    assert "claude-sonnet-default" in job.error


# ---------------------------------------------------------------------------
# 7. job.queued event emitted with reason="connectivity" when route OPEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queued_event_emitted_on_open():
    audit = MockAuditClient()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=_make_context_packager(),
        connectivity_monitor=monitor,
    )

    with patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())):
        job_id = await _submit_and_run(jm)

    queued_events = audit.get_events_by_type("job.queued")
    assert len(queued_events) == 1
    assert queued_events[0]["payload"]["reason"] == "connectivity"


# ---------------------------------------------------------------------------
# 8. job.failed event emitted with error_class="route_unavailable" when OPEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_event_emitted_on_open():
    audit = MockAuditClient()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=_make_context_packager(),
        connectivity_monitor=monitor,
    )

    with patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())):
        job_id = await _submit_and_run(jm)

    failed_events = audit.get_events_by_type("job.failed")
    assert len(failed_events) == 1
    assert failed_events[0]["payload"]["error_class"] == "route_unavailable"


# ---------------------------------------------------------------------------
# 9. Context Packager is NOT called when route is OPEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_packager_not_called_on_open():
    audit = MockAuditClient()
    packager = _make_context_packager()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=packager, connectivity_monitor=monitor,
    )

    with patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())):
        job_id = await _submit_and_run(jm)

    packager.package.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Egress gateway HTTP call is NOT made when route is OPEN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_egress_not_called_on_open():
    audit = MockAuditClient()
    packager = _make_context_packager()
    monitor = _make_connectivity_monitor(available=False)
    jm = JobManager(
        audit_client=audit, context_packager=packager, connectivity_monitor=monitor,
    )

    egress_client = _make_egress_client()
    with (
        patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())),
        patch("job_manager.httpx.AsyncClient", return_value=egress_client),
    ):
        job_id = await _submit_and_run(jm)

    egress_client.post.assert_not_called()
