"""Unit tests for the worker-proxy sidecar service.

All Docker interactions are mocked — no running daemon required.

Uses importlib to load worker-proxy modules by explicit file path, avoiding
name collisions with orchestrator/models.py and orchestrator/main.py that
share sys.path during full-suite runs.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import datetime
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Explicit file-path imports for worker-proxy modules
# ---------------------------------------------------------------------------
_WP_DIR = os.path.join(os.path.dirname(__file__), "..", "worker-proxy")


def _load_wp(name: str):
    spec = importlib.util.spec_from_file_location(
        f"wp_{name}", os.path.join(_WP_DIR, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"wp_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_wp_registry = _load_wp("registry")

# models.py does "from registry import ..." — point sys.modules["registry"]
# at the worker-proxy version before importing models.
_orig_registry = sys.modules.get("registry")
sys.modules["registry"] = _wp_registry
_wp_models = _load_wp("models")
if _orig_registry is not None:
    sys.modules["registry"] = _orig_registry
else:
    sys.modules.pop("registry", None)

# main.py does "from models import ..." — temporarily point sys.modules
# at the worker-proxy version so the import resolves correctly.
_orig_models = sys.modules.get("models")
sys.modules["models"] = _wp_models
_orig_registry = sys.modules.get("registry")
sys.modules["registry"] = _wp_registry
_wp_main = _load_wp("main")
if _orig_models is not None:
    sys.modules["models"] = _orig_models
else:
    sys.modules.pop("models", None)
if _orig_registry is not None:
    sys.modules["registry"] = _orig_registry
else:
    sys.modules.pop("registry", None)

ContainerRunRequest = _wp_models.ContainerRunRequest
ContainerRunResponse = _wp_models.ContainerRunResponse


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Reset the module-level Docker client between tests."""
    _wp_main._docker_client = None
    _wp_main._host_sandbox_base = None
    yield
    _wp_main._docker_client = None
    _wp_main._host_sandbox_base = None


@pytest.fixture(autouse=True)
def _test_registry():
    """Install a test registry before each test runs.

    The Pydantic validators read from registry.get_active(); without an active
    registry, every request would fail with RuntimeError. TestClient does not
    invoke the lifespan hook (no `with` block), so we set the module's
    active registry directly.
    """
    reg = _wp_registry.Registry(
        approved_images=frozenset({"drnt-worker:latest"}),
        allowed_networks=frozenset({"drnt-internal"}),
        allowed_volume_names=frozenset({"drnt-sandbox-workdir"}),
        caps=_wp_registry.RegistryCaps(
            mem_limit_max="4g",
            pids_limit_max=512,
            wall_timeout_max=3600,
        ),
    )
    _wp_registry.set_active(reg)
    yield reg
    _wp_registry._active = None


@pytest.fixture()
def mock_docker_client():
    client = MagicMock()
    client.ping.return_value = True

    with patch.object(_wp_main, "get_docker_client", return_value=client):
        yield client


@pytest.fixture()
def test_client(mock_docker_client):
    """FastAPI TestClient with mocked Docker."""
    from fastapi.testclient import TestClient
    return TestClient(_wp_main.app)


def _make_run_request(**overrides) -> dict:
    """Build a minimal valid ContainerRunRequest body.

    All fields satisfy the registry allowlist and field validators. Overrides
    let individual tests punch one field invalid to exercise a specific rule.
    """
    base = dict(
        image="drnt-worker:latest",
        name="drnt-worker-test-abc123",
        labels={"drnt.role": "worker", "drnt.job_id": "j1"},
        environment={"OLLAMA_URL": "http://ollama:11434"},
        volumes={"/var/drnt/workers/j1/inbox": {"bind": "/inbox", "mode": "ro"}},
        security_opt=["no-new-privileges"],
        mem_limit="256m",
        pids_limit=64,
        cap_drop=["ALL"],
        read_only=True,
        tmpfs={"/tmp": "size=64m,noexec"},
        network_mode="none",
        wall_timeout=60,
    )
    base.update(overrides)
    return base


def _fake_container(exit_code: int = 0, logs: bytes = b""):
    """Return a MagicMock that quacks like a Docker container."""
    ctr = MagicMock()
    ctr.id = "abc123def456"
    ctr.wait.return_value = {"StatusCode": exit_code}
    ctr.logs.return_value = logs
    return ctr


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, test_client, mock_docker_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        mock_docker_client.ping.assert_called_once()

    def test_health_daemon_unavailable(self, test_client, mock_docker_client):
        mock_docker_client.ping.side_effect = Exception("connection refused")
        resp = test_client.get("/health")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Image check endpoint
# ---------------------------------------------------------------------------

class TestImageCheck:
    def test_image_exists(self, test_client, mock_docker_client):
        mock_docker_client.images.get.return_value = MagicMock()
        resp = test_client.get("/images/drnt-worker:latest")
        assert resp.status_code == 200
        assert resp.json() == {"exists": True}

    def test_image_not_found(self, test_client, mock_docker_client):
        import docker.errors
        mock_docker_client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        resp = test_client.get("/images/no-such-image:latest")
        assert resp.status_code == 200
        assert resp.json() == {"exists": False}

    def test_image_check_error(self, test_client, mock_docker_client):
        mock_docker_client.images.get.side_effect = Exception("daemon error")
        resp = test_client.get("/images/drnt-worker:latest")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Container run endpoint
# ---------------------------------------------------------------------------

class TestContainerRun:
    def test_run_success(self, test_client, mock_docker_client):
        ctr = _fake_container(exit_code=0)
        mock_docker_client.containers.create.return_value = ctr

        resp = test_client.post("/containers/run", json=_make_run_request())
        assert resp.status_code == 200

        body = resp.json()
        assert body["status"] == "completed"
        assert body["exit_code"] == 0
        assert body["container_id"] == "abc123def456"
        assert body["logs"] is None

        # Container must be removed in finally block
        ctr.remove.assert_called_once_with(force=True)

    def test_run_failure_returns_logs(self, test_client, mock_docker_client):
        ctr = _fake_container(exit_code=1, logs=b"Error: model not found\n")
        mock_docker_client.containers.create.return_value = ctr

        resp = test_client.post("/containers/run", json=_make_run_request())
        assert resp.status_code == 200

        body = resp.json()
        assert body["status"] == "failed"
        assert body["exit_code"] == 1
        assert "model not found" in body["logs"]
        ctr.remove.assert_called_once_with(force=True)

    def test_run_timeout_kills_container(self, test_client, mock_docker_client):
        import requests.exceptions

        ctr = _fake_container()
        ctr.wait.side_effect = requests.exceptions.ConnectionError("timed out")
        mock_docker_client.containers.create.return_value = ctr

        resp = test_client.post("/containers/run", json=_make_run_request(wall_timeout=5))
        assert resp.status_code == 200

        body = resp.json()
        assert body["status"] == "timeout"
        assert body["exit_code"] == -1
        ctr.kill.assert_called_once()
        ctr.remove.assert_called_once_with(force=True)

    def test_run_timeout_read_timeout(self, test_client, mock_docker_client):
        import requests.exceptions

        ctr = _fake_container()
        ctr.wait.side_effect = requests.exceptions.ReadTimeout("read timed out")
        mock_docker_client.containers.create.return_value = ctr

        resp = test_client.post("/containers/run", json=_make_run_request(wall_timeout=5))
        assert resp.status_code == 200
        assert resp.json()["status"] == "timeout"

    def test_run_cleanup_on_create_failure(self, test_client, mock_docker_client):
        import docker.errors
        mock_docker_client.containers.create.side_effect = docker.errors.APIError("out of memory")

        resp = test_client.post("/containers/run", json=_make_run_request())
        assert resp.status_code == 500

    def test_run_image_not_found(self, test_client, mock_docker_client):
        import docker.errors
        mock_docker_client.containers.create.side_effect = docker.errors.ImageNotFound("nope")

        resp = test_client.post("/containers/run", json=_make_run_request())
        assert resp.status_code == 404

    def test_run_container_removed_even_on_failure(self, test_client, mock_docker_client):
        """Container.remove(force=True) must be called even when wait raises."""
        ctr = _fake_container()
        ctr.wait.side_effect = Exception("unexpected")
        mock_docker_client.containers.create.return_value = ctr

        # The exception becomes a 500, but container must still be removed
        resp = test_client.post("/containers/run", json=_make_run_request())
        assert resp.status_code == 500
        ctr.remove.assert_called_once_with(force=True)

    def test_run_network_mode_none(self, test_client, mock_docker_client):
        ctr = _fake_container(exit_code=0)
        mock_docker_client.containers.create.return_value = ctr

        test_client.post("/containers/run", json=_make_run_request(network_mode="none"))

        kwargs = mock_docker_client.containers.create.call_args
        assert kwargs[1]["network_mode"] == "none"
        assert "network" not in kwargs[1]

    def test_run_named_network(self, test_client, mock_docker_client):
        ctr = _fake_container(exit_code=0)
        mock_docker_client.containers.create.return_value = ctr

        test_client.post("/containers/run",
                         json=_make_run_request(network_mode=None, network="drnt-internal"))

        kwargs = mock_docker_client.containers.create.call_args
        assert kwargs[1]["network"] == "drnt-internal"
        assert "network_mode" not in kwargs[1]


# ---------------------------------------------------------------------------
# Container config translation
# ---------------------------------------------------------------------------

class TestRequestValidation:
    """Default-deny allowlist enforcement on ContainerRunRequest.

    Replaces the previous pass-through translation tests (retired in the
    Phase 2B v0.2 hardening): the proxy no longer forwards any submitted
    configuration — it rejects requests that deviate from the locked-down
    blueprint shape.
    """

    def test_legitimate_blueprint_shape_accepted(self, test_client, mock_docker_client):
        ctr = _fake_container(exit_code=0)
        mock_docker_client.containers.create.return_value = ctr

        resp = test_client.post("/containers/run", json=_make_run_request())
        assert resp.status_code == 200

    def test_cap_drop_empty_rejected(self, test_client, mock_docker_client):
        resp = test_client.post("/containers/run", json=_make_run_request(cap_drop=[]))
        assert resp.status_code == 422
        assert "cap_drop" in resp.text

    def test_read_only_false_rejected(self, test_client, mock_docker_client):
        resp = test_client.post("/containers/run", json=_make_run_request(read_only=False))
        assert resp.status_code == 422
        assert "read_only" in resp.text

    def test_security_opt_missing_no_new_privileges_rejected(
        self, test_client, mock_docker_client
    ):
        resp = test_client.post(
            "/containers/run",
            json=_make_run_request(security_opt=["seccomp=/tmp/x.json"]),
        )
        assert resp.status_code == 422
        assert "no-new-privileges" in resp.text

    def test_network_mode_host_rejected(self, test_client, mock_docker_client):
        resp = test_client.post(
            "/containers/run", json=_make_run_request(network_mode="host"),
        )
        assert resp.status_code == 422
        assert "network_mode" in resp.text

    def test_volume_outside_sandbox_rejected(self, test_client, mock_docker_client):
        resp = test_client.post(
            "/containers/run",
            json=_make_run_request(volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            }),
        )
        assert resp.status_code == 422
        assert "/var/run/docker.sock" in resp.text

    def test_image_not_in_registry_rejected(self, test_client, mock_docker_client):
        resp = test_client.post(
            "/containers/run", json=_make_run_request(image="alpine:latest"),
        )
        assert resp.status_code == 422
        assert "approved_images" in resp.text

    def test_mem_limit_above_cap_rejected(self, test_client, mock_docker_client):
        resp = test_client.post(
            "/containers/run", json=_make_run_request(mem_limit="16g"),
        )
        assert resp.status_code == 422
        assert "mem_limit" in resp.text

    def test_label_missing_worker_marker_rejected(self, test_client, mock_docker_client):
        resp = test_client.post(
            "/containers/run",
            json=_make_run_request(labels={"drnt.job_id": "j1"}),
        )
        assert resp.status_code == 422
        assert "drnt.role" in resp.text


# ---------------------------------------------------------------------------
# Container logs endpoint
# ---------------------------------------------------------------------------

class TestContainerLogs:
    def test_get_logs(self, test_client, mock_docker_client):
        ctr = MagicMock()
        ctr.logs.return_value = b"line1\nline2\n"
        mock_docker_client.containers.get.return_value = ctr

        resp = test_client.get("/containers/abc123/logs?tail=50")
        assert resp.status_code == 200
        body = resp.json()
        assert body["container_id"] == "abc123"
        assert "line1" in body["logs"]
        ctr.logs.assert_called_once_with(tail=50)

    def test_get_logs_not_found(self, test_client, mock_docker_client):
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

        resp = test_client.get("/containers/abc123/logs")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Container delete endpoint
# ---------------------------------------------------------------------------

class TestContainerDelete:
    def test_remove_container(self, test_client, mock_docker_client):
        ctr = MagicMock()
        mock_docker_client.containers.get.return_value = ctr

        resp = test_client.delete("/containers/abc123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "removed"
        ctr.remove.assert_called_once_with(force=True)

    def test_remove_not_found(self, test_client, mock_docker_client):
        import docker.errors
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("gone")

        resp = test_client.delete("/containers/abc123")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Host sandbox resolution
# ---------------------------------------------------------------------------

class TestSandboxResolution:
    def test_resolve_host_sandbox_base(self):
        mock_vol = MagicMock()
        mock_vol.attrs = {"Mountpoint": "/var/lib/docker/volumes/sandbox/_data"}
        mock_client = MagicMock()
        mock_client.volumes.get.return_value = mock_vol

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            result = _wp_main.resolve_host_sandbox_base()

        assert result == "/var/lib/docker/volumes/sandbox/_data"

    def test_resolve_host_sandbox_base_failure(self):
        mock_client = MagicMock()
        mock_client.volumes.get.side_effect = Exception("not found")

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            result = _wp_main.resolve_host_sandbox_base()

        assert result is None


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------

def _stopped_container(ctr_id: str, finished_seconds_ago: float):
    """Create a mock container that exited `finished_seconds_ago` seconds ago."""
    ctr = MagicMock()
    ctr.id = ctr_id
    finished_at = datetime.datetime.fromtimestamp(
        time.time() - finished_seconds_ago, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    ctr.attrs = {"State": {"FinishedAt": finished_at}}
    return ctr


class TestGarbageCollection:
    def test_gc_removes_old_stopped_containers(self):
        old_ctr = _stopped_container("old_container_1", finished_seconds_ago=600)
        mock_client = MagicMock()
        mock_client.containers.list.return_value = [old_ctr]

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            removed = _wp_main._gc_sweep()

        assert removed == 1
        old_ctr.remove.assert_called_once_with(force=True)

    def test_gc_ignores_recently_stopped_containers(self):
        recent_ctr = _stopped_container("recent_123456", finished_seconds_ago=60)
        mock_client = MagicMock()
        mock_client.containers.list.return_value = [recent_ctr]

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            removed = _wp_main._gc_sweep()

        assert removed == 0
        recent_ctr.remove.assert_not_called()

    def test_gc_handles_no_containers(self):
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            removed = _wp_main._gc_sweep()

        assert removed == 0

    def test_gc_handles_docker_list_error(self):
        mock_client = MagicMock()
        mock_client.containers.list.side_effect = Exception("daemon unavailable")

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            removed = _wp_main._gc_sweep()

        assert removed == 0

    def test_gc_handles_per_container_remove_error(self):
        """GC continues processing other containers if one removal fails."""
        old1 = _stopped_container("fail_container", finished_seconds_ago=600)
        old1.remove.side_effect = Exception("busy")
        old2 = _stopped_container("good_container", finished_seconds_ago=600)

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [old1, old2]

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            removed = _wp_main._gc_sweep()

        # old2 should still be removed even though old1 failed
        assert removed == 1
        old2.remove.assert_called_once_with(force=True)

    def test_gc_uses_correct_filters(self):
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []

        with patch.object(_wp_main, "get_docker_client", return_value=mock_client):
            _wp_main._gc_sweep()

        mock_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": "drnt.role=worker", "status": "exited"},
        )
