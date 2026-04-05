"""Worker executor unit tests — sandbox, container execution via sidecar,
health checks, execution events, lifecycle execute_in_worker bridge,
and worker_agent I/O.

Uses MockHttpxClient and MockResponse to mock the worker-proxy sidecar
HTTP API, isolating each concern without a running Docker daemon.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))

from worker_executor import WorkerExecutor, WorkerExecutionError, WorkerResult
from worker_lifecycle import WorkerLifecycle, WorkerPreparationError
from test_helpers import MockAuditClient

# UUIDv7 pattern for event validation
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Test helpers — sidecar HTTP mocks
# ---------------------------------------------------------------------------


class MockResponse:
    """Stand-in for httpx.Response."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data


class MockHttpxClient:
    """Captures POST requests to the sidecar and returns configurable responses."""

    def __init__(self, response: MockResponse | None = None):
        self._response = response or MockResponse()
        self.last_post_json: dict[str, Any] | None = None
        self.last_post_url: str | None = None

    def post(self, url: str, json: dict | None = None, **kwargs) -> MockResponse:
        self.last_post_url = url
        self.last_post_json = json
        return self._response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# 1. TestSandboxDirectory (5 tests)
# ---------------------------------------------------------------------------


class TestSandboxDirectory:
    """Sandbox directory preparation, task writing, result reading, cleanup."""

    def test_prepare_creates_inbox_and_outbox(self, tmp_path):
        executor = WorkerExecutor(sandbox_base_dir=str(tmp_path))
        sandbox = executor._prepare_sandbox_dir("job-001", "worker-001")

        assert (sandbox / "inbox").is_dir()
        assert (sandbox / "outbox").is_dir()

    def test_write_task_creates_json(self, tmp_path):
        executor = WorkerExecutor(sandbox_base_dir=str(tmp_path))
        sandbox = executor._prepare_sandbox_dir("job-002", "worker-002")

        task = {
            "task_id": "job-002",
            "task_type": "text_generation",
            "payload": {"prompt": "hello", "model": "llama3.1:8b"},
        }
        executor._write_task(sandbox, task)

        task_file = sandbox / "inbox" / "task.json"
        assert task_file.exists()
        loaded = json.loads(task_file.read_text())
        assert loaded["task_id"] == "job-002"
        assert loaded["task_type"] == "text_generation"
        assert loaded["payload"]["prompt"] == "hello"

    def test_read_result_returns_dict(self, tmp_path):
        executor = WorkerExecutor(sandbox_base_dir=str(tmp_path))
        sandbox = executor._prepare_sandbox_dir("job-003", "worker-003")

        result_data = {
            "task_id": "job-003",
            "status": "success",
            "result": "The weather is sunny.",
            "token_count_in": 10,
            "token_count_out": 20,
            "model": "llama3.1:8b",
            "wall_seconds": 1.5,
        }
        result_file = sandbox / "outbox" / "result.json"
        result_file.write_text(json.dumps(result_data))

        loaded = executor._read_result(sandbox)
        assert loaded["status"] == "success"
        assert loaded["result"] == "The weather is sunny."
        assert loaded["token_count_in"] == 10

    def test_read_result_raises_on_missing_file(self, tmp_path):
        executor = WorkerExecutor(sandbox_base_dir=str(tmp_path))
        sandbox = executor._prepare_sandbox_dir("job-004", "worker-004")

        with pytest.raises(WorkerExecutionError, match="result.json not found"):
            executor._read_result(sandbox)

    def test_cleanup_removes_directory(self, tmp_path):
        executor = WorkerExecutor(sandbox_base_dir=str(tmp_path))
        sandbox = executor._prepare_sandbox_dir("job-005", "worker-005")
        assert sandbox.exists()

        executor._cleanup_sandbox_dir(sandbox)
        assert not sandbox.exists()


# ---------------------------------------------------------------------------
# 2. TestContainerExecution (6 tests)
# ---------------------------------------------------------------------------


class TestContainerExecution:
    """Container execution via worker-proxy sidecar HTTP API."""

    def _make_executor(self, tmp_path, sidecar_response=None):
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        mock_client = MockHttpxClient(sidecar_response or MockResponse(
            json_data={"container_id": "fake-container-abc123", "exit_code": 0, "status": "completed"},
        ))
        return executor, mock_client

    @pytest.mark.asyncio
    async def test_execute_success_via_sidecar(self, tmp_path):
        sidecar_resp = MockResponse(json_data={
            "container_id": "fake-container-abc123",
            "exit_code": 0,
            "status": "completed",
        })
        executor, mock_client = self._make_executor(tmp_path, sidecar_resp)

        # Pre-write result.json that the "container" would produce
        sandbox = executor._prepare_sandbox_dir("job-exec-01", "worker-exec-01")
        result_data = {
            "task_id": "job-exec-01",
            "status": "success",
            "result": "Hello world",
            "token_count_in": 5,
            "token_count_out": 10,
            "model": "llama3.1:8b",
            "wall_seconds": 2.0,
        }
        (sandbox / "outbox" / "result.json").write_text(json.dumps(result_data))

        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                result = await executor.execute(
                    worker_id="worker-exec-01",
                    job_id="job-exec-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-exec-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi", "model": "llama3.1:8b"}},
                    resource_config={"memory_limit": "256m", "cpu_period": 100000},
                    security_config={"cap_drop": ["ALL"], "read_only_rootfs": True},
                )

        assert isinstance(result, WorkerResult)
        assert result.success is True
        assert result.response_text == "Hello world"
        assert result.token_count_in == 5
        assert result.token_count_out == 10
        assert result.model == "llama3.1:8b"
        assert result.container_id == "fake-container-abc123"

    @pytest.mark.asyncio
    async def test_container_failure_returns_success_false(self, tmp_path):
        sidecar_resp = MockResponse(json_data={
            "container_id": "fake-container-fail",
            "exit_code": 1,
            "status": "failed",
            "logs": "Segfault",
        })
        executor, mock_client = self._make_executor(tmp_path, sidecar_resp)

        sandbox = executor._prepare_sandbox_dir("job-fail-01", "worker-fail-01")

        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                result = await executor.execute(
                    worker_id="worker-fail-01",
                    job_id="job-fail-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-fail-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={},
                )

        assert result.success is False
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_security_config_applied(self, tmp_path):
        sidecar_resp = MockResponse(json_data={
            "container_id": "fake-sec", "exit_code": 0, "status": "completed",
        })
        executor, mock_client = self._make_executor(tmp_path, sidecar_resp)

        sandbox = executor._prepare_sandbox_dir("job-sec-01", "worker-sec-01")
        (sandbox / "outbox" / "result.json").write_text(
            json.dumps({"status": "success", "result": "ok"})
        )

        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                await executor.execute(
                    worker_id="worker-sec-01",
                    job_id="job-sec-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-sec-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={"mem_limit": "256m", "pids_limit": 256},
                )

        body = mock_client.last_post_json
        assert body["cap_drop"] == ["ALL"]
        assert body["read_only"] is True
        assert body["security_opt"] == ["no-new-privileges"]
        assert body["mem_limit"] == "256m"
        assert body["pids_limit"] == 256

    @pytest.mark.asyncio
    async def test_inbox_mounted_ro_outbox_mounted_rw(self, tmp_path):
        sidecar_resp = MockResponse(json_data={
            "container_id": "fake-mnt", "exit_code": 0, "status": "completed",
        })
        executor, mock_client = self._make_executor(tmp_path, sidecar_resp)

        sandbox = executor._prepare_sandbox_dir("job-mnt-01", "worker-mnt-01")
        (sandbox / "outbox" / "result.json").write_text(
            json.dumps({"status": "success", "result": "ok"})
        )

        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                await executor.execute(
                    worker_id="worker-mnt-01",
                    job_id="job-mnt-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-mnt-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={},
                )

        volumes = mock_client.last_post_json["volumes"]
        # Find the inbox and outbox bindings
        inbox_binding = None
        outbox_binding = None
        for host_path, config in volumes.items():
            if config["bind"] == "/inbox":
                inbox_binding = config
            elif config["bind"] == "/outbox":
                outbox_binding = config

        assert inbox_binding is not None
        assert inbox_binding["mode"] == "ro"
        assert outbox_binding is not None
        assert outbox_binding["mode"] == "rw"

    @pytest.mark.asyncio
    async def test_environment_vars_set_correctly(self, tmp_path):
        sidecar_resp = MockResponse(json_data={
            "container_id": "fake-env", "exit_code": 0, "status": "completed",
        })
        executor, mock_client = self._make_executor(tmp_path, sidecar_resp)

        sandbox = executor._prepare_sandbox_dir("job-env-01", "worker-env-01")
        (sandbox / "outbox" / "result.json").write_text(
            json.dumps({"status": "success", "result": "ok"})
        )

        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                await executor.execute(
                    worker_id="worker-env-01",
                    job_id="job-env-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-env-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={},
                )

        env = mock_client.last_post_json["environment"]
        assert env["OLLAMA_URL"] == "http://ollama:11434"
        assert env["DRNT_WORKER_ID"] == "worker-env-01"
        assert env["DRNT_JOB_ID"] == "job-env-01"
        assert env["DRNT_CAPABILITY"] == "route.local"

    @pytest.mark.asyncio
    async def test_cleanup_on_no_result(self, tmp_path):
        """Sandbox is cleaned up even when no result.json is produced."""
        sidecar_resp = MockResponse(json_data={
            "container_id": "fake-cleanup", "exit_code": 0, "status": "completed",
        })
        executor, mock_client = self._make_executor(tmp_path, sidecar_resp)

        sandbox = executor._prepare_sandbox_dir("job-cleanup-01", "worker-cleanup-01")
        # No result.json — simulates container that produced no output

        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                result = await executor.execute(
                    worker_id="worker-cleanup-01",
                    job_id="job-cleanup-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-cleanup-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={},
                )

        # Sandbox should have been cleaned up
        assert not sandbox.exists()
        # Result should indicate failure (no result.json)
        assert result.success is False


# ---------------------------------------------------------------------------
# 3. TestDockerChecks (3 tests)
# ---------------------------------------------------------------------------


class TestDockerChecks:
    """Sidecar availability and image existence checks."""

    def test_check_docker_available_true(self, tmp_path):
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        with patch("worker_executor.httpx.get", return_value=MockResponse(status_code=200)):
            assert executor.check_docker_available() is True

    def test_check_docker_available_false(self, tmp_path):
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        with patch("worker_executor.httpx.get", side_effect=httpx.ConnectError("refused")):
            assert executor.check_docker_available() is False

    def test_check_image_exists(self, tmp_path):
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        # Image exists
        with patch("worker_executor.httpx.get", return_value=MockResponse(
            json_data={"exists": True}
        )):
            assert executor.check_image_exists("drnt-worker:latest") is True

        # Image does not exist
        with patch("worker_executor.httpx.get", return_value=MockResponse(
            json_data={"exists": False}
        )):
            assert executor.check_image_exists("nonexistent:v1") is False


# ---------------------------------------------------------------------------
# 4. TestWorkerExecutionEvents (4 tests)
# ---------------------------------------------------------------------------


class TestWorkerExecutionEvents:
    """Event structure for worker.execution_started and worker.execution_completed."""

    def test_started_event_structure(self):
        from events import event_worker_execution_started

        evt = event_worker_execution_started(
            worker_id="w-001",
            job_id="j-001",
            capability_id="route.local",
            container_id="ctr-abc",
            image="drnt-worker:latest",
            task_type="text_generation",
        )
        assert evt["event_type"] == "worker.execution_started"
        assert evt["source"] == "orchestrator"
        assert evt["schema_version"] == "2.0"
        assert UUID_V7_RE.match(evt["source_event_id"])
        assert evt["capability_id"] == "route.local"
        payload = evt["payload"]
        assert payload["worker_id"] == "w-001"
        assert payload["job_id"] == "j-001"
        assert payload["capability_id"] == "route.local"
        assert payload["container_id"] == "ctr-abc"
        assert payload["image"] == "drnt-worker:latest"
        assert payload["task_type"] == "text_generation"

    def test_completed_success_event(self):
        from events import event_worker_execution_completed

        evt = event_worker_execution_completed(
            worker_id="w-002",
            job_id="j-002",
            capability_id="route.local",
            container_id="ctr-def",
            exit_code=0,
            success=True,
            latency_ms=1500,
            token_count_in=10,
            token_count_out=25,
        )
        assert evt["event_type"] == "worker.execution_completed"
        payload = evt["payload"]
        assert payload["success"] is True
        assert payload["exit_code"] == 0
        assert payload["latency_ms"] == 1500
        assert payload["token_count_in"] == 10
        assert payload["token_count_out"] == 25
        assert "error" not in payload

    def test_completed_failure_with_error(self):
        from events import event_worker_execution_completed

        evt = event_worker_execution_completed(
            worker_id="w-003",
            job_id="j-003",
            capability_id="route.local",
            container_id="ctr-ghi",
            exit_code=1,
            success=False,
            latency_ms=200,
            error="ollama request failed: Connection refused",
        )
        payload = evt["payload"]
        assert payload["success"] is False
        assert payload["exit_code"] == 1
        assert payload["error"] == "ollama request failed: Connection refused"

    def test_both_events_have_capability_id(self):
        from events import event_worker_execution_started, event_worker_execution_completed

        started = event_worker_execution_started(
            worker_id="w-004", job_id="j-004",
            capability_id="route.cloud.general",
            container_id="pending", image="drnt-worker:latest",
            task_type="text_generation",
        )
        completed = event_worker_execution_completed(
            worker_id="w-004", job_id="j-004",
            capability_id="route.cloud.general",
            container_id="ctr-xyz", exit_code=0,
            success=True, latency_ms=3000,
        )

        assert started["capability_id"] == "route.cloud.general"
        assert completed["capability_id"] == "route.cloud.general"
        assert started["payload"]["capability_id"] == "route.cloud.general"
        assert completed["payload"]["capability_id"] == "route.cloud.general"


# ---------------------------------------------------------------------------
# 5. TestLifecycleExecuteInWorker (6 tests)
# ---------------------------------------------------------------------------


class _FakeWorkerExecutor:
    """Minimal async executor for lifecycle integration tests."""

    def __init__(self, result: WorkerResult | None = None):
        self._result = result or WorkerResult(
            success=True,
            response_text="Test response",
            token_count_in=5,
            token_count_out=15,
            latency_ms=1200,
            model="llama3.1:8b",
            exit_code=0,
            container_id="fake-ctr-001",
        )
        self.last_kwargs: dict[str, Any] = {}

    async def execute(self, **kwargs) -> WorkerResult:
        self.last_kwargs = kwargs
        return self._result


def _make_lifecycle_with_executor(tmp_path, executor=None):
    """Build a WorkerLifecycle wired with a fake executor."""
    from capability_registry import CapabilityRegistry
    from capability_state import CapabilityStateManager
    from blueprint_engine import BlueprintEngine
    from manifest_validator import ManifestValidator, build_egress_endpoints_from_routes
    from worker_lifecycle import EgressPolicyStore
    from egress_proxy import EgressPolicy

    caps = {
        "route.local": {
            "capability_id": "route.local",
            "capability_name": "Local Routing",
            "capability_type": "governing",
            "desired_wal_level": 0,
            "max_wal": 3,
            "declared_pipeline": [],
            "provider_dependencies": None,
            "action_policies": {
                "0": {"dispatch_local": {"review_gate": "none"}},
                "1": {"dispatch_local": {"review_gate": "none"}},
                "2": {"dispatch_local": {"review_gate": "none"}},
                "3": {"dispatch_local": {"review_gate": "none"}},
            },
            "promotion_criteria": {"0_to_1": None, "1_to_2": None, "2_to_3": None},
        },
    }
    caps_path = os.path.join(str(tmp_path), "capabilities.json")
    with open(caps_path, "w") as f:
        json.dump(caps, f)

    registry = CapabilityRegistry(config_path=caps_path)
    registry.load()

    state_path = os.path.join(str(tmp_path), "capabilities.state.json")
    state_mgr = CapabilityStateManager(state_path=state_path)
    state_mgr.initialize_from_registry(registry)

    egress_endpoints = build_egress_endpoints_from_routes([])
    validator = ManifestValidator(
        registry=registry, state_manager=state_mgr, egress_endpoints=egress_endpoints,
    )
    engine = BlueprintEngine()
    policy_store = EgressPolicyStore()
    audit = MockAuditClient()

    fake_executor = executor or _FakeWorkerExecutor()

    lifecycle = WorkerLifecycle(
        manifest_validator=validator,
        blueprint_engine=engine,
        capability_registry=registry,
        egress_policy_store=policy_store,
        audit_client=audit,
        state_manager=state_mgr,
        worker_executor=fake_executor,
    )
    return lifecycle, audit, fake_executor


def _make_job(capability_id="route.local", **overrides):
    from models import Job, JobStatus
    return Job(
        job_id=overrides.get("job_id", "test-job-001"),
        raw_input="What is the weather?",
        input_modality="text",
        device="phone",
        status=overrides.get("status", JobStatus.classified.value),
        governing_capability_id=capability_id,
    )


class TestLifecycleExecuteInWorker:
    """execute_in_worker bridges lifecycle and executor."""

    def test_has_executor_true(self, tmp_path):
        lifecycle, _, _ = _make_lifecycle_with_executor(tmp_path)
        assert lifecycle.has_executor is True

    def test_has_executor_false(self, tmp_path):
        lifecycle, _, _ = _make_lifecycle_with_executor(tmp_path)
        lifecycle._worker_executor = None
        assert lifecycle.has_executor is False

    @pytest.mark.asyncio
    async def test_execute_returns_result(self, tmp_path):
        lifecycle, audit, _ = _make_lifecycle_with_executor(tmp_path)
        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        result = await lifecycle.execute_in_worker(ctx, prompt="hello", model="llama3.1:8b")

        assert result["response_text"] == "Test response"
        assert result["token_count_in"] == 5
        assert result["token_count_out"] == 15
        assert result["latency_ms"] == 1200
        assert result["model"] == "llama3.1:8b"
        assert result["container_id"] == "fake-ctr-001"

    @pytest.mark.asyncio
    async def test_emits_started_event(self, tmp_path):
        lifecycle, audit, _ = _make_lifecycle_with_executor(tmp_path)
        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        await lifecycle.execute_in_worker(ctx, prompt="hello")

        started = audit.get_events_by_type("worker.execution_started")
        assert len(started) == 1
        assert started[0]["payload"]["worker_id"] == ctx.worker_id
        assert started[0]["payload"]["job_id"] == "test-job-001"
        assert started[0]["payload"]["task_type"] == "text_generation"

    @pytest.mark.asyncio
    async def test_emits_completed_event(self, tmp_path):
        lifecycle, audit, _ = _make_lifecycle_with_executor(tmp_path)
        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        await lifecycle.execute_in_worker(ctx, prompt="hello")

        completed = audit.get_events_by_type("worker.execution_completed")
        assert len(completed) == 1
        payload = completed[0]["payload"]
        assert payload["success"] is True
        assert payload["exit_code"] == 0
        assert payload["container_id"] == "fake-ctr-001"
        assert payload["latency_ms"] == 1200

    @pytest.mark.asyncio
    async def test_raises_without_executor(self, tmp_path):
        lifecycle, audit, _ = _make_lifecycle_with_executor(tmp_path)
        lifecycle._worker_executor = None

        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)

        with pytest.raises(WorkerPreparationError, match="no worker_executor"):
            await lifecycle.execute_in_worker(ctx, prompt="hello")


# ---------------------------------------------------------------------------
# 6. TestWorkerAgent (5 tests)
# ---------------------------------------------------------------------------


class TestWorkerAgent:
    """worker_agent.py read_task / write_result and dispatch logic."""

    def test_read_valid_task(self, tmp_path):
        from worker_agent import read_task, INBOX

        task_data = {
            "task_id": "agent-001",
            "task_type": "text_generation",
            "payload": {"prompt": "hello", "model": "llama3.1:8b"},
        }
        inbox_file = tmp_path / "task.json"
        inbox_file.write_text(json.dumps(task_data))

        with patch("worker_agent.INBOX", str(inbox_file)):
            result = read_task()

        assert result["task_id"] == "agent-001"
        assert result["task_type"] == "text_generation"
        assert result["payload"]["prompt"] == "hello"

    def test_missing_fields_handled(self, tmp_path):
        """handle_text_generation returns error when prompt is missing."""
        from worker_agent import handle_text_generation

        task = {
            "task_id": "agent-002",
            "task_type": "text_generation",
            "payload": {},  # no prompt
        }
        result = handle_text_generation(task)
        assert result["status"] == "error"
        assert "prompt is required" in result["error"]

    def test_unsupported_type(self, tmp_path):
        """TASK_HANDLERS returns None for unsupported task_type."""
        from worker_agent import TASK_HANDLERS

        handler = TASK_HANDLERS.get("image_generation")
        assert handler is None

    def test_file_not_found(self, tmp_path):
        """read_task raises FileNotFoundError for missing inbox."""
        from worker_agent import read_task

        with patch("worker_agent.INBOX", str(tmp_path / "nonexistent" / "task.json")):
            with pytest.raises(FileNotFoundError):
                read_task()

    def test_write_result(self, tmp_path):
        from worker_agent import write_result, OUTBOX

        outbox_file = tmp_path / "result.json"

        result_data = {
            "task_id": "agent-005",
            "status": "success",
            "result": "The answer is 42.",
            "token_count_in": 8,
            "token_count_out": 12,
            "model": "llama3.1:8b",
            "wall_seconds": 3.14,
        }

        with patch("worker_agent.OUTBOX", str(outbox_file)):
            write_result(result_data)

        assert outbox_file.exists()
        loaded = json.loads(outbox_file.read_text())
        assert loaded["task_id"] == "agent-005"
        assert loaded["status"] == "success"
        assert loaded["result"] == "The answer is 42."
        assert loaded["token_count_in"] == 8
        assert loaded["token_count_out"] == 12
        assert loaded["wall_seconds"] == 3.14


# ---------------------------------------------------------------------------
# 7. TestSeccompAndNetworkIsolation (6 tests)
# ---------------------------------------------------------------------------


class TestSeccompAndNetworkIsolation:
    """Seccomp profile and network isolation plumbing through executor to sidecar."""

    def _make_executor(self, tmp_path, sidecar_response=None):
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        mock_client = MockHttpxClient(sidecar_response or MockResponse(
            json_data={"container_id": "fake-sn", "exit_code": 0, "status": "completed"},
        ))
        return executor, mock_client

    async def _run_execute(self, executor, mock_client, tmp_path, security_config, suffix="01"):
        sandbox = executor._prepare_sandbox_dir(f"job-sn-{suffix}", f"worker-sn-{suffix}")
        (sandbox / "outbox" / "result.json").write_text(
            json.dumps({"status": "success", "result": "ok"})
        )
        with patch.object(executor, "_prepare_sandbox_dir", return_value=sandbox):
            with patch("worker_executor.httpx.Client", return_value=mock_client):
                return await executor.execute(
                    worker_id=f"worker-sn-{suffix}",
                    job_id=f"job-sn-{suffix}",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": f"job-sn-{suffix}", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config=security_config,
                )

    @pytest.mark.asyncio
    async def test_seccomp_profile_in_security_opt(self, tmp_path):
        """seccomp_profile in security_config appears in security_opt."""
        executor, mock_client = self._make_executor(tmp_path)
        await self._run_execute(executor, mock_client, tmp_path, {
            "seccomp_profile": "/var/drnt/config/seccomp-default.json",
        }, suffix="01")
        body = mock_client.last_post_json
        assert "no-new-privileges" in body["security_opt"]
        assert "seccomp=/var/drnt/config/seccomp-default.json" in body["security_opt"]

    @pytest.mark.asyncio
    async def test_security_opt_without_seccomp(self, tmp_path):
        """Without seccomp_profile, security_opt has only no-new-privileges."""
        executor, mock_client = self._make_executor(tmp_path)
        await self._run_execute(executor, mock_client, tmp_path, {}, suffix="02")
        body = mock_client.last_post_json
        assert body["security_opt"] == ["no-new-privileges"]

    @pytest.mark.asyncio
    async def test_network_mode_none_uses_network_mode_param(self, tmp_path):
        """network_mode='none' sets network_mode param, not network."""
        executor, mock_client = self._make_executor(tmp_path)
        await self._run_execute(executor, mock_client, tmp_path, {"network_mode": "none"}, suffix="03")
        body = mock_client.last_post_json
        assert body.get("network_mode") == "none"
        assert "network" not in body

    @pytest.mark.asyncio
    async def test_named_network_uses_network_param(self, tmp_path):
        """Non-'none' network_mode uses network param."""
        executor, mock_client = self._make_executor(tmp_path)
        await self._run_execute(executor, mock_client, tmp_path, {
            "network_mode": "drnt-egress-proxy",
        }, suffix="04")
        body = mock_client.last_post_json
        assert body.get("network") == "drnt-egress-proxy"
        assert "network_mode" not in body

    @pytest.mark.asyncio
    async def test_default_network_mode_is_none(self, tmp_path):
        """When no network_mode in security_config, default to 'none' (fail closed)."""
        executor, mock_client = self._make_executor(tmp_path)
        await self._run_execute(executor, mock_client, tmp_path, {}, suffix="05")
        body = mock_client.last_post_json
        assert body.get("network_mode") == "none"
        assert "network" not in body

    @pytest.mark.asyncio
    async def test_seccomp_and_network_combined(self, tmp_path):
        """Both seccomp and network_mode flow through together."""
        executor, mock_client = self._make_executor(tmp_path)
        await self._run_execute(executor, mock_client, tmp_path, {
            "seccomp_profile": "/var/drnt/config/seccomp-default.json",
            "network_mode": "drnt-egress-proxy",
        }, suffix="06")
        body = mock_client.last_post_json
        assert "seccomp=/var/drnt/config/seccomp-default.json" in body["security_opt"]
        assert body.get("network") == "drnt-egress-proxy"


# ---------------------------------------------------------------------------
# 8. TestLifecycleSeccompNetworkPlumbing (3 tests)
# ---------------------------------------------------------------------------


class TestLifecycleSeccompNetworkPlumbing:
    """Lifecycle passes seccomp and network_mode through to executor."""

    @pytest.mark.asyncio
    async def test_passes_seccomp_profile_from_env(self, tmp_path):
        """execute_in_worker passes DRNT_SECCOMP_PROFILE to executor when path is non-default."""
        lifecycle, audit, executor = _make_lifecycle_with_executor(tmp_path)
        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)
        # Set a non-default seccomp path so the env var is honoured
        ctx.blueprint.security_config.seccomp_profile_path = "/custom/seccomp.json"
        audit.clear()

        with patch.dict(os.environ, {"DRNT_SECCOMP_PROFILE": "/var/drnt/config/seccomp-default.json"}):
            await lifecycle.execute_in_worker(ctx, prompt="hello")

        sc = executor.last_kwargs["security_config"]
        assert sc["seccomp_profile"] == "/var/drnt/config/seccomp-default.json"

    @pytest.mark.asyncio
    async def test_passes_network_mode_none_for_empty_egress(self, tmp_path):
        """Workers with empty egress_allow get network_mode='none'."""
        lifecycle, audit, executor = _make_lifecycle_with_executor(tmp_path)
        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        await lifecycle.execute_in_worker(ctx, prompt="hello")

        sc = executor.last_kwargs["security_config"]
        assert sc["network_mode"] == "none"

    @pytest.mark.asyncio
    async def test_passes_egress_proxy_network_for_nonempty_egress(self, tmp_path):
        """Workers with non-empty egress_allow get network_mode='drnt-internal'."""
        from egress_proxy import EgressPolicy

        lifecycle, audit, executor = _make_lifecycle_with_executor(tmp_path)
        lifecycle._egress_store.set("route.local", EgressPolicy(
            capability_id="route.local",
            allowed_endpoints=["api.example.com:443"],
            rate_limit_rpm=30,
        ))
        # Authorize the endpoint in the manifest validator
        lifecycle._validator._egress_endpoints["route.local"] = ["api.example.com:443"]

        job = _make_job()
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        await lifecycle.execute_in_worker(ctx, prompt="hello")

        sc = executor.last_kwargs["security_config"]
        assert sc["network_mode"] == "drnt-internal"


# ---------------------------------------------------------------------------
# 9. TestSidecarFailureModes (3 tests)
# ---------------------------------------------------------------------------


class TestSidecarFailureModes:
    """Fail-closed behavior when the worker-proxy sidecar is unavailable."""

    @pytest.mark.asyncio
    async def test_sidecar_unreachable_raises(self, tmp_path):
        """If the sidecar is unreachable, execution fails closed."""
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")

        with patch("worker_executor.httpx.Client", return_value=mock_client):
            with pytest.raises(WorkerExecutionError, match="Worker proxy unreachable"):
                await executor.execute(
                    worker_id="worker-fail-01",
                    job_id="job-fail-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-fail-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={},
                )

    @pytest.mark.asyncio
    async def test_sidecar_timeout_response(self, tmp_path):
        """Sidecar returns timeout status → WorkerResult with timeout error."""
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        sidecar_resp = MockResponse(json_data={
            "container_id": "ctr-timeout",
            "exit_code": -1,
            "status": "timeout",
        })
        mock_client = MockHttpxClient(sidecar_resp)

        with patch("worker_executor.httpx.Client", return_value=mock_client):
            result = await executor.execute(
                worker_id="worker-to-01",
                job_id="job-to-01",
                capability_id="route.local",
                image="drnt-worker:latest",
                task_payload={"task_id": "job-to-01", "task_type": "text_generation",
                              "payload": {"prompt": "hi"}},
                resource_config={},
                security_config={},
                wall_timeout=60,
            )

        assert result.success is False
        assert "timed out" in result.error
        assert result.container_id == "ctr-timeout"

    @pytest.mark.asyncio
    async def test_sidecar_error_response(self, tmp_path):
        """Sidecar returns HTTP 500 → WorkerExecutionError."""
        executor = WorkerExecutor(
            sandbox_base_dir=str(tmp_path),
            worker_proxy_url="http://test-proxy:9100",
        )
        sidecar_resp = MockResponse(status_code=500, text="Internal Server Error")
        mock_client = MockHttpxClient(sidecar_resp)

        with patch("worker_executor.httpx.Client", return_value=mock_client):
            with pytest.raises(WorkerExecutionError, match="Sidecar error 500"):
                await executor.execute(
                    worker_id="worker-err-01",
                    job_id="job-err-01",
                    capability_id="route.local",
                    image="drnt-worker:latest",
                    task_payload={"task_id": "job-err-01", "task_type": "text_generation",
                                  "payload": {"prompt": "hi"}},
                    resource_config={},
                    security_config={},
                )
