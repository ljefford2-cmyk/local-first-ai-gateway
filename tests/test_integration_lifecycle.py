"""Integration tests exercising cross-component flows with mocked external
dependencies.

These tests verify the complete job lifecycle — submission through routing,
execution, and audit event verification — using FastAPI TestClient with mocked
Ollama and egress gateway dependencies. Each test exercises the full async
pipeline: HTTP submission → background worker → classification → permission
check → dispatch → response → delivery, with audit events captured and
verified at each step.

Full end-to-end tests against a running Docker Compose stack are a v2 concern.

Usage:
    pytest tests/test_integration_lifecycle.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

# Add orchestrator to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from classifier import ClassificationResult, LocalResponseResult
from job_manager import JobManager
from models import JobStatus


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class MockAuditClient:
    """Captures audit events in-memory for verification.

    Enhanced with health_check() for compatibility with the /health endpoint.
    """

    def __init__(self):
        self.events: list[dict] = []

    async def emit_durable(self, event: dict) -> bool:
        self.events.append(event)
        return True

    async def emit_best_effort(self, event: dict) -> None:
        self.events.append(event)

    async def health_check(self) -> bool:
        return True

    def get_events_by_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e.get("event_type") == event_type]

    def get_events_for_job(self, job_id: str) -> list[dict]:
        return [e for e in self.events if e.get("job_id") == job_id]

    def clear(self):
        self.events.clear()


def _local_classification() -> ClassificationResult:
    return ClassificationResult(
        category="quick_lookup",
        local_capable=True,
        routing="local",
        recommended_model="local",
        confidence=0.95,
        capability_id="route.local",
        candidate_models=["llama3.1:8b"],
        route_id="ollama-llama3-local",
    )


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


def _local_response() -> LocalResponseResult:
    return LocalResponseResult(
        text="The capital of France is Paris.",
        token_count_in=10,
        token_count_out=8,
    )


class _MockContextPackagerResult:
    context_package_id = "ctx-test-001"
    assembled_payload_hash = "abc123def456"
    assembled_payload = "Packaged: test prompt"


class _MockContextPackager:
    async def package(self, **kwargs):
        return _MockContextPackagerResult()


class _FailingWorkerLifecycle:
    """Worker lifecycle whose executor raises during execution."""

    has_executor = True

    async def prepare_worker(self, job):
        ctx = MagicMock()
        ctx.worker_id = "mock-w-fail"
        ctx.status = "active"
        return ctx

    async def execute_in_worker(self, context=None, prompt=None, model=None, task_type=None):
        raise RuntimeError("Ollama connection refused")

    async def teardown_worker(self, ctx):
        pass


def _mock_egress_response() -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "status": "ok",
        "response_text": "Cloud response from Claude.",
        "latency_ms": 500,
        "token_count_in": 15,
        "token_count_out": 30,
        "cost_estimate_usd": 0.01,
        "result_id": "result-cloud-001",
        "response_hash": "cloudhash123",
        "finish_reason": "stop",
    }
    return resp


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    """Poll GET /jobs/{id} until a terminal state or timeout."""
    terminal = {"delivered", "failed", "revoked"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/jobs/{job_id}")
        data = resp.json()
        if data["status"] in terminal:
            return data
        time.sleep(0.2)
    return client.get(f"/jobs/{job_id}").json()


def _collect_statuses(client: TestClient, job_id: str, timeout: float = 10.0) -> list[str]:
    """Poll GET /jobs/{id} and collect all distinct observed status values."""
    terminal = {"delivered", "failed", "revoked"}
    statuses: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/jobs/{job_id}")
        status = resp.json()["status"]
        if not statuses or statuses[-1] != status:
            statuses.append(status)
        if status in terminal:
            break
        time.sleep(0.05)
    return statuses


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def lifecycle_env():
    """Provide a (TestClient, MockAuditClient, JobManager) tuple.

    Creates a FastAPI app with a simplified lifespan that wires a JobManager
    to a MockAuditClient and starts the background worker loop. External
    dependencies (Ollama, egress, filesystem) are patched per-test.
    """
    import main as main_module

    mock_audit = MockAuditClient()

    @asynccontextmanager
    async def test_lifespan(app):
        jm = JobManager(audit_client=mock_audit)
        main_module.job_manager = jm
        main_module.audit_client = mock_audit
        await jm.start()
        yield
        await jm.stop()
        main_module.job_manager = None

    orig_lifespan = main_module.app.router.lifespan_context
    main_module.app.router.lifespan_context = test_lifespan

    try:
        with patch("job_manager._write_result"):
            with TestClient(main_module.app) as client:
                yield client, mock_audit, main_module.job_manager
    finally:
        main_module.app.router.lifespan_context = orig_lifespan


# ---------------------------------------------------------------------------
# 1. Local job full lifecycle
# ---------------------------------------------------------------------------


class TestLocalJobFullLifecycle:
    """Submit a text job → route local → Ollama generate → delivered."""

    def test_local_job_full_lifecycle(self, lifecycle_env):
        client, audit, jm = lifecycle_env

        with (
            patch("job_manager.classify", new=AsyncMock(return_value=_local_classification())),
            patch("job_manager.generate_local_response", new=AsyncMock(return_value=_local_response())),
        ):
            resp = client.post("/jobs", json={
                "raw_input": "What is the capital of France?",
                "input_modality": "text",
                "device": "watch",
            })
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            result = _wait_for_terminal(client, job_id)

        assert result["status"] == "delivered"
        assert result["result"] is not None
        assert "Paris" in result["result"]

        # Verify all expected audit events are present
        job_events = audit.get_events_for_job(job_id)
        event_types = [e["event_type"] for e in job_events]

        expected_sequence = [
            "job.submitted",
            "job.classified",
            "wal.permission_check",
            "job.dispatched",
            "model.response",
            "job.response_received",
            "job.delivered",
        ]
        for exp in expected_sequence:
            assert exp in event_types, f"Missing '{exp}' in audit trail: {event_types}"

        # Verify ordering
        indices = {et: event_types.index(et) for et in expected_sequence}
        for i in range(1, len(expected_sequence)):
            prev, curr = expected_sequence[i - 1], expected_sequence[i]
            assert indices[prev] < indices[curr], (
                f"Event order violation: '{prev}' (idx {indices[prev]}) "
                f"should precede '{curr}' (idx {indices[curr]})"
            )


# ---------------------------------------------------------------------------
# 2. Cloud job full lifecycle
# ---------------------------------------------------------------------------


class TestCloudJobFullLifecycle:
    """Submit a text job → route cloud → egress gateway → delivered."""

    def test_cloud_job_full_lifecycle(self, lifecycle_env):
        client, audit, jm = lifecycle_env

        # Wire mock context packager into the running JobManager
        jm._packager = _MockContextPackager()

        # Mock egress gateway HTTP call
        mock_egress_resp = _mock_egress_response()
        mock_httpx_client = AsyncMock()
        mock_httpx_client.post.return_value = mock_egress_resp
        mock_httpx_client.__aenter__ = AsyncMock(return_value=mock_httpx_client)
        mock_httpx_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("job_manager.classify", new=AsyncMock(return_value=_cloud_classification())),
            patch("job_manager.httpx.AsyncClient", return_value=mock_httpx_client),
        ):
            resp = client.post("/jobs", json={
                "raw_input": "Explain quantum entanglement.",
                "input_modality": "text",
                "device": "phone",
            })
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            result = _wait_for_terminal(client, job_id)

        assert result["status"] == "delivered"
        assert result["result"] is not None

        # Verify cloud-path audit events
        job_events = audit.get_events_for_job(job_id)
        event_types = [e["event_type"] for e in job_events]

        assert "job.submitted" in event_types
        assert "job.classified" in event_types
        assert "job.dispatched" in event_types
        assert "job.delivered" in event_types

        # Verify context_package_id propagated through dispatch
        dispatched = [e for e in job_events if e["event_type"] == "job.dispatched"]
        assert len(dispatched) == 1
        assert dispatched[0]["payload"]["context_package_id"] == "ctx-test-001"


# ---------------------------------------------------------------------------
# 3. Audit event ordering
# ---------------------------------------------------------------------------


class TestAuditEventOrdering:
    """Audit events have monotonically increasing timestamps and valid IDs."""

    def test_audit_event_ordering(self, lifecycle_env):
        client, audit, jm = lifecycle_env

        with (
            patch("job_manager.classify", new=AsyncMock(return_value=_local_classification())),
            patch("job_manager.generate_local_response", new=AsyncMock(return_value=_local_response())),
        ):
            resp = client.post("/jobs", json={
                "raw_input": "What is 2 plus 2?",
                "input_modality": "text",
                "device": "watch",
            })
            job_id = resp.json()["job_id"]
            _wait_for_terminal(client, job_id)

        job_events = audit.get_events_for_job(job_id)
        assert len(job_events) >= 5, f"Expected ≥5 events, got {len(job_events)}"

        # Timestamps must be monotonically non-decreasing
        timestamps = [e["timestamp"] for e in job_events]
        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1], (
                f"Timestamp at index {i} ({timestamps[i]}) < "
                f"previous ({timestamps[i - 1]})"
            )

        # Each event must have a unique source_event_id
        seen_ids: set[str] = set()
        for evt in job_events:
            sid = evt.get("source_event_id")
            assert sid is not None, (
                f"Event '{evt['event_type']}' missing source_event_id"
            )
            assert len(sid) > 0, (
                f"Event '{evt['event_type']}' has empty source_event_id"
            )
            assert sid not in seen_ids, f"Duplicate source_event_id: {sid}"
            seen_ids.add(sid)


# ---------------------------------------------------------------------------
# 4. Job status transitions
# ---------------------------------------------------------------------------


class TestJobStatusTransitions:
    """Job status progresses through the expected states."""

    def test_job_status_transitions(self, lifecycle_env):
        client, audit, jm = lifecycle_env

        # Add small delays so polling can observe intermediate states
        async def slow_classify(raw_input):
            await asyncio.sleep(0.3)
            return _local_classification()

        async def slow_generate(raw_input):
            await asyncio.sleep(0.3)
            return _local_response()

        with (
            patch("job_manager.classify", new=slow_classify),
            patch("job_manager.generate_local_response", new=slow_generate),
        ):
            resp = client.post("/jobs", json={
                "raw_input": "Test status transitions",
                "input_modality": "text",
                "device": "watch",
            })
            job_id = resp.json()["job_id"]
            statuses = _collect_statuses(client, job_id)

        # Must start with submitted and end with delivered
        assert statuses[0] == "submitted"
        assert statuses[-1] == "delivered"

        # All observed statuses must be in the valid progression order
        valid_order = [
            "submitted", "classified", "dispatched",
            "response_received", "delivered",
        ]
        for s in statuses:
            assert s in valid_order, f"Unexpected status: {s}"

        # No regressions: each observed status must come later in the
        # valid progression than the previous one
        for i in range(1, len(statuses)):
            assert valid_order.index(statuses[i]) > valid_order.index(statuses[i - 1]), (
                f"Status regression: {statuses[i - 1]} → {statuses[i]}"
            )


# ---------------------------------------------------------------------------
# 5. Failed job lifecycle
# ---------------------------------------------------------------------------


class TestFailedJobLifecycle:
    """Job with failing worker execution reaches 'failed' status."""

    def test_failed_job_lifecycle(self, lifecycle_env):
        client, audit, jm = lifecycle_env

        # Wire a worker lifecycle whose executor raises during execution.
        # This triggers the Branch 1 (worker container) exception handler
        # which emits a job.failed event with error_class="worker_execution_failed".
        jm._worker_lifecycle = _FailingWorkerLifecycle()

        with patch("job_manager.classify", new=AsyncMock(return_value=_local_classification())):
            resp = client.post("/jobs", json={
                "raw_input": "This should fail",
                "input_modality": "text",
                "device": "watch",
            })
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            result = _wait_for_terminal(client, job_id)

        assert result["status"] == "failed"
        assert result["error"] is not None

        # Verify job.failed event was emitted
        job_events = audit.get_events_for_job(job_id)
        failed_events = [
            e for e in job_events if e["event_type"] == "job.failed"
        ]
        assert len(failed_events) >= 1, (
            f"No job.failed event emitted. Events: "
            f"{[e['event_type'] for e in job_events]}"
        )
        assert failed_events[0]["payload"]["error_class"] == "worker_execution_failed"


# ---------------------------------------------------------------------------
# 6. Override after delivery
# ---------------------------------------------------------------------------


class TestOverrideAfterDelivery:
    """Submit, wait for delivery, then cancel. Verify revoked status and events."""

    def test_override_after_delivery(self, lifecycle_env):
        client, audit, jm = lifecycle_env

        with (
            patch("job_manager.classify", new=AsyncMock(return_value=_local_classification())),
            patch("job_manager.generate_local_response", new=AsyncMock(return_value=_local_response())),
        ):
            # Step 1: submit and wait for delivery
            resp = client.post("/jobs", json={
                "raw_input": "What is the speed of light?",
                "input_modality": "text",
                "device": "watch",
            })
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            delivered = _wait_for_terminal(client, job_id)
            assert delivered["status"] == "delivered"

            # Step 2: POST cancel override on the delivered job
            override_resp = client.post(f"/jobs/{job_id}/override", json={
                "override_type": "cancel",
                "target": "routing",
                "detail": "test revocation",
                "device": "watch",
            })
            assert override_resp.status_code == 200
            override_data = override_resp.json()
            assert override_data["status"] == "revoked"

        # Step 3: verify job is now revoked
        final = client.get(f"/jobs/{job_id}").json()
        assert final["status"] == "revoked"

        # Step 4: verify human.override and job.revoked events
        job_events = audit.get_events_for_job(job_id)
        event_types = [e["event_type"] for e in job_events]

        assert "human.override" in event_types
        assert "job.revoked" in event_types

        # human.override event has correct payload
        override_events = [
            e for e in job_events if e["event_type"] == "human.override"
        ]
        assert override_events[0]["payload"]["override_type"] == "cancel"
        assert override_events[0]["source"] == "human"

        # job.revoked event references the override
        revoked_events = [
            e for e in job_events if e["event_type"] == "job.revoked"
        ]
        assert revoked_events[0]["payload"]["reason"] == "user_cancel"
