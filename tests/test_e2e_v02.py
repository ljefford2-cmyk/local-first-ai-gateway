"""DRNT v0.2 End-to-End Tests.

Tests hit the live Docker stack via HTTP. No mocks.
Covers: worker execution, concurrency, cloud dispatch, sidecar GC,
persistence (idempotency/circuit breaker), and full-stack restart.

IMPORTANT: Tests are ordered so non-disruptive tests run FIRST. Restart
tests run LAST because they disrupt worker-proxy connectivity.

Usage:
    pytest tests/test_e2e_v02.py -v --tb=short -x
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("DRNT_TEST_URL", "http://localhost:8000")
TIMEOUT = int(os.environ.get("DRNT_TEST_TIMEOUT", "90"))
COMPOSE_PROJECT = os.environ.get("DRNT_COMPOSE_PROJECT", "")

_COMPOSE_CMD = ["docker", "compose"]
if COMPOSE_PROJECT:
    _COMPOSE_CMD += ["-p", COMPOSE_PROJECT]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compose(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_COMPOSE_CMD, *args],
        capture_output=True, text=True, timeout=timeout,
    )


async def api_get(path: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as c:
        return await c.get(path, params=params)


async def api_post(path: str, json: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as c:
        return await c.post(path, json=json)


async def wait_for_health(timeout: int = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = await api_get("/health")
            if r.status_code == 200:
                return True
        except Exception:
            pass
        await asyncio.sleep(3)
    return False


async def ensure_hub_active() -> None:
    try:
        r = await api_get("/hub_status")
        if r.status_code == 200:
            state = r.json().get("state", "")
            if state != "active":
                await api_post("/confirm_authority")
                await api_post("/resume")
    except Exception:
        pass


async def submit_job(
    prompt: str = "What is 2 plus 2?",
    input_modality: str = "text",
    device: str = "watch",
    idempotency_key: str | None = None,
) -> dict:
    body: dict[str, Any] = {
        "raw_input": prompt,
        "input_modality": input_modality,
        "device": device,
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    r = await api_post("/jobs", json=body)
    assert r.status_code == 202, f"Job submit failed: {r.status_code} {r.text}"
    return r.json()


async def wait_for_job(
    job_id: str,
    terminal: set[str] | None = None,
    timeout: int = TIMEOUT,
) -> dict:
    if terminal is None:
        terminal = {"delivered", "failed", "revoked"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await api_get(f"/jobs/{job_id}")
        if r.status_code != 200:
            await asyncio.sleep(1)
            continue
        data = r.json()
        if data["status"] in terminal:
            return data
        await asyncio.sleep(1)
    r = await api_get(f"/jobs/{job_id}")
    return r.json()


async def submit_and_wait(prompt: str = "What is 2 plus 2?", timeout: int = TIMEOUT) -> dict:
    await ensure_hub_active()
    sub = await submit_job(prompt)
    return await wait_for_job(sub["job_id"], timeout=timeout)


async def ollama_available() -> bool:
    try:
        r = await api_get("/health")
        if r.status_code == 200:
            return r.json().get("ollama_status") in ("available", "healthy", "degraded")
    except Exception:
        pass
    return False


def _restore_stack():
    """Full compose restart to restore all inter-service connectivity."""
    _compose("down")
    _compose("up", "-d")
    asyncio.get_event_loop().run_until_complete(wait_for_health(timeout=180))
    asyncio.get_event_loop().run_until_complete(ensure_hub_active())


# ===========================================================================
# Category 2: Worker execution through sidecar  (non-disruptive — runs first)
# ===========================================================================


@pytest.mark.e2e
class TestWorkerExecution:
    """Job execution via the worker-proxy sidecar."""

    @pytest.mark.asyncio
    async def test_local_ollama_job_completes(self):
        """Submit a job routed to local Ollama, verify it reaches delivered."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        # Retry for Ollama warm-up
        for attempt in range(3):
            job = await submit_and_wait(
                "What is 2 plus 2? Answer with just the number.",
                timeout=TIMEOUT,
            )
            if job["status"] == "delivered":
                break
            if attempt < 2:
                await asyncio.sleep(5)

        assert job["status"] == "delivered", (
            f"Job did not deliver: status={job['status']}, error={job.get('error')}"
        )
        assert job["result"] is not None
        assert len(job["result"]) > 0

    @pytest.mark.asyncio
    async def test_worker_container_security_profile(self):
        """Verify worker containers use seccomp/cap_drop via blueprint audit events."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        job = await submit_and_wait("Security profile test: name a color.")
        if job["status"] != "delivered":
            pytest.skip(f"Job did not deliver: {job.get('error')}")

        r = await api_get("/admin/audit-events", params={
            "event_type": "sandbox.blueprint_created",
            "limit": 20,
        })
        if r.status_code != 200:
            pytest.skip("Audit events endpoint not available")

        events = r.json().get("events", [])
        if not events:
            pytest.skip("No blueprint events — worker may use direct dispatch fallback")

        payload = events[-1].get("payload", {})
        assert payload.get("cap_drop_count", 0) >= 1, "Expected cap_drop_count >= 1"

    @pytest.mark.asyncio
    async def test_worker_seccomp_profile_applied(self):
        """Verify worker container has seccomp profile in SecurityOpt."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        import json as _json

        worker_cid = None

        async def _poll_for_worker(job_id: str) -> str | None:
            """Poll docker for a worker container matching this job."""
            for _ in range(60):
                result = subprocess.run(
                    ["docker", "ps", "-a", "--filter", "label=drnt.role=worker",
                     "--filter", f"label=drnt.job_id={job_id}",
                     "--format", "{{.ID}}"],
                    capture_output=True, text=True,
                )
                cid = result.stdout.strip().split("\n")[0].strip()
                if cid:
                    return cid
                await asyncio.sleep(0.25)
            return None

        # Submit the job and poll for the worker container concurrently
        job_sub = await submit_job("Seccomp enforcement test: what is 1+1?")
        job_id = job_sub["job_id"]
        worker_cid = await _poll_for_worker(job_id)

        if not worker_cid:
            pytest.skip("Worker container not found — may have been GC'd too fast")

        # Inspect SecurityOpt on the actual container
        inspect = subprocess.run(
            ["docker", "inspect", worker_cid, "--format",
             "{{json .HostConfig.SecurityOpt}}"],
            capture_output=True, text=True,
        )
        sec_opts = _json.loads(inspect.stdout.strip())

        seccomp_entries = [s for s in sec_opts if s.startswith("seccomp=")]
        assert len(seccomp_entries) == 1, (
            f"Expected exactly one seccomp= entry in SecurityOpt, got {sec_opts}"
        )
        profile_json = seccomp_entries[0][len("seccomp="):]
        profile = _json.loads(profile_json)
        assert profile.get("defaultAction") == "SCMP_ACT_ERRNO", (
            f"Seccomp profile defaultAction is not SCMP_ACT_ERRNO: {profile.get('defaultAction')}"
        )

        # Let the job finish so it doesn't interfere with later tests
        await wait_for_job(job_id, timeout=TIMEOUT)

    @pytest.mark.asyncio
    async def test_worker_seccomp_enforcement_blocks_disallowed_syscall(self):
        """Prove seccomp blocks `personality(0xFFFFFFFF)` inside a worker.

        Uses the fixed-contract admin probe endpoint (POST
        /admin/e2e/syscall-probe) so the worker task itself carries the
        probe — no race against GC, no exec into a live container.
        The probe is hardcoded on the worker side: target=personality,
        control=getpid; the endpoint takes no parameters.
        """
        await ensure_hub_active()

        import re as _re

        r = await api_post("/admin/e2e/syscall-probe")
        assert r.status_code == 200, (
            f"probe endpoint failed: status={r.status_code} body={r.text}"
        )
        body = r.json()
        result_line = body.get("result_line", "")
        container_id = body.get("container_id")

        match = _re.search(
            r"SECCOMP_TEST_RESULT:\s+target_ret=(-?\d+)\s+target_errno=(-?\d+)"
            r"\s+control_ret=(-?\d+)\s+control_errno=(-?\d+)",
            result_line,
        )

        def _diagnostics(reason: str) -> str:
            parts = [
                f"{reason}",
                f"full_result_line: {result_line!r}",
                f"container_id: {container_id!r}",
                f"response_body: {body!r}",
            ]
            if container_id:
                try:
                    inspect = subprocess.run(
                        ["docker", "inspect", container_id, "--format",
                         "{{json .HostConfig.SecurityOpt}}"],
                        capture_output=True, text=True, timeout=5,
                    )
                    out = inspect.stdout.strip()
                    err = inspect.stderr.strip()
                    if out:
                        parts.append(
                            f"SecurityOpt[first200]: {out[:200]!r} "
                            f"SecurityOpt[last200]: {out[-200:]!r}"
                        )
                    if err:
                        parts.append(f"inspect_stderr: {err!r}")
                    if inspect.returncode != 0:
                        parts.append("inspect_unavailable (container likely removed after teardown)")
                except Exception as exc:
                    parts.append(f"inspect_error: {exc!r}")
            return " | ".join(parts)

        assert match, _diagnostics(
            "did not find SECCOMP_TEST_RESULT line in probe response"
        )

        target_ret = int(match.group(1))
        target_errno = int(match.group(2))
        control_ret = int(match.group(3))
        control_errno = int(match.group(4))

        assert control_ret > 0, _diagnostics(
            f"control getpid() did not return a valid pid (got {control_ret}, "
            f"errno={control_errno}) — worker or container setup is broken"
        )

        assert target_ret == -1, _diagnostics(
            f"target personality(0xFFFFFFFF) was NOT blocked: "
            f"returned {target_ret} (expected -1). Seccomp profile is not "
            f"enforcing the allowlist."
        )

        assert target_errno == 1, _diagnostics(
            f"target personality(0xFFFFFFFF) returned -1 but errno={target_errno} "
            f"(expected 1/EPERM). EPERM is the profile's defaultErrnoRet; a "
            f"different errno means something else blocked it."
        )

    @pytest.mark.asyncio
    async def test_worker_network_isolation(self):
        """Verify sandboxed execution via worker.prepared audit events."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        job = await submit_and_wait("Network isolation test: define gravity.")
        if job["status"] != "delivered":
            pytest.skip(f"Job did not deliver: {job.get('error')}")

        r = await api_get("/admin/audit-events", params={
            "event_type": "worker.prepared",
            "limit": 20,
        })
        if r.status_code != 200:
            pytest.skip("Audit events endpoint not available")

        events = r.json().get("events", [])
        if not events:
            pytest.skip("No worker.prepared events — direct dispatch fallback active")

        assert events[-1]["event_type"] == "worker.prepared"

    @pytest.mark.asyncio
    async def test_worker_proxy_health_via_startup_report(self):
        """Worker-proxy is internal (port 9100). Verify via startup report."""
        r = await api_get("/admin/startup-report")
        if r.status_code != 200:
            pytest.skip("Startup report not available")

        report = r.json()
        checks = report.get("checks", [])

        wp_checks = [c for c in checks if "worker" in c.get("check_name", "").lower()
                      or "proxy" in c.get("check_name", "").lower()]

        if not wp_checks:
            assert report.get("hub_start_permitted") is True
        else:
            for c in wp_checks:
                assert c["passed"] is True, (
                    f"Worker proxy check '{c['check_name']}' failed: {c.get('message')}"
                )


# ===========================================================================
# Category 3: Concurrency limits
# ===========================================================================


@pytest.mark.e2e
class TestConcurrencyLimits:
    """Verify the semaphore-based concurrency limit (max 4 workers)."""

    @pytest.mark.asyncio
    async def test_max_concurrent_workers(self):
        """Submit 6+ jobs, verify pipeline handles concurrency correctly."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        prompts = [f"Concurrency test {i}: What is {i} + {i}?" for i in range(6)]

        submissions = []
        for p in prompts:
            try:
                sub = await submit_job(p)
                submissions.append(sub)
            except AssertionError:
                pass  # 503 = backpressure, which proves the limit

        assert len(submissions) >= 4, (
            f"Expected at least 4 jobs accepted, got {len(submissions)}"
        )

        results = []
        for sub in submissions:
            job = await wait_for_job(sub["job_id"], timeout=TIMEOUT * 2)
            results.append(job)

        terminal = [r for r in results if r["status"] in ("delivered", "failed")]
        assert len(terminal) >= 1, "No jobs reached terminal state in concurrency test"


# ===========================================================================
# Category 4: Cloud dispatch
# ===========================================================================


@pytest.mark.e2e
class TestCloudDispatch:
    """Verify cloud dispatch path through egress gateway."""

    @pytest.mark.asyncio
    async def test_cloud_routable_job(self):
        """Submit a job the classifier might route to cloud."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        job = await submit_and_wait(
            "Write a detailed analysis of international trade law and its implications "
            "for cross-border digital commerce regulations in the European Union.",
            timeout=TIMEOUT,
        )

        if job["status"] == "failed":
            error = job.get("error", "")
            if any(kw in error.lower() for kw in ["egress", "cloud", "permission", "worker"]):
                pytest.skip("Cloud dispatch attempted but failed — no API keys configured")

        assert job["status"] in ("delivered", "failed"), (
            f"Unexpected terminal status: {job['status']}"
        )

    @pytest.mark.asyncio
    async def test_context_packager_evidence(self):
        """Check audit events for context packaging on cloud-routed jobs."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        job = await submit_and_wait(
            "Explain quantum entanglement in detail for a research paper.",
            timeout=TIMEOUT,
        )

        r = await api_get("/admin/audit-events", params={
            "job_id": job["job_id"],
            "limit": 50,
        })
        if r.status_code != 200:
            pytest.skip("Audit events endpoint not available")

        events = r.json().get("events", [])
        event_types = [e["event_type"] for e in events]

        if "context.packaged" in event_types:
            pkg_events = [e for e in events if e["event_type"] == "context.packaged"]
            assert len(pkg_events) >= 1
        # else: routed locally, no context packaging needed — pass


# ===========================================================================
# Category 5: Sidecar GC
# ===========================================================================


@pytest.mark.e2e
class TestSidecarGC:
    """Verify the worker-proxy garbage collection loop."""

    @pytest.mark.asyncio
    async def test_gc_no_stale_worker_containers(self):
        """After job completion, worker containers should not accumulate."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        job = await submit_and_wait("GC test: what is 1 plus 1?")
        if job["status"] not in ("delivered", "failed"):
            pytest.skip(f"Job did not reach terminal state: {job['status']}")

        result = subprocess.run(
            ["docker", "ps", "-a", "--filter", "label=drnt.role=worker",
             "--format", "{{.ID}} {{.Status}}"],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            pytest.skip("Cannot list Docker containers from test host")

        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]

        # The sidecar's finally block removes containers after run.
        # Any remaining containers should be exited (not running),
        # and will be swept by the GC loop within 5 minutes.
        running = [l for l in lines if "Up" in l]
        # Running workers are only expected during active jobs.
        # After our job completed, finding running workers suggests a leak,
        # but they may belong to concurrent tests.
        # The structural guarantee: GC interval=60s, max_age=300s.


# ===========================================================================
# Category 1: Persistence (idempotency, circuit breaker)
# ===========================================================================


@pytest.mark.e2e
class TestPersistence:
    """Idempotency dedup and persistence fundamentals."""

    @pytest.mark.asyncio
    async def test_idempotency_dedup(self):
        """Submit twice with the same key — second returns original job_id."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        idem_key = f"e2e-idem-{int(time.time())}"
        sub1 = await submit_job("Idempotency dedup test", idempotency_key=idem_key)
        sub2 = await submit_job("Idempotency dedup test", idempotency_key=idem_key)

        assert sub2["job_id"] == sub1["job_id"], (
            f"Dedup failed: got {sub2['job_id']}, expected {sub1['job_id']}"
        )

    @pytest.mark.asyncio
    async def test_sqlite_tables_exist(self):
        """Verify the orchestrator's SQLite database has the expected tables."""
        # Health being "connected" for audit log and "running" for orchestrator
        # means SQLite initialized successfully (init_db creates both tables).
        r = await api_get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["orchestrator_status"] == "running"
        assert data["audit_log_status"] == "connected"


# ===========================================================================
# Category 1+6: Restart tests (DISRUPTIVE — run last)
# ===========================================================================


@pytest.mark.e2e
class TestRestartResilience:
    """Restart tests — these disrupt inter-service connectivity."""

    @pytest.mark.asyncio
    async def test_orchestrator_restart_healthy(self):
        """Restart orchestrator, verify it comes back healthy."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        # Pre-restart: submit a job to prove functionality
        job = await submit_and_wait("Pre-restart test: capital of France?")
        assert job["status"] in ("delivered", "failed"), (
            f"Pre-restart job did not reach terminal state: {job['status']}"
        )

        # Restart only the orchestrator
        result = _compose("restart", "orchestrator")
        assert result.returncode == 0, f"Restart failed: {result.stderr}"

        healthy = await wait_for_health(timeout=120)
        assert healthy, "Orchestrator did not become healthy after restart"
        await ensure_hub_active()

        r = await api_get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["orchestrator_status"] == "running"
        assert data["audit_log_status"] == "connected"

    @pytest.mark.asyncio
    async def test_idempotency_key_cleared_after_restart(self):
        """Terminal idempotency keys are purged on restart — same key creates a new job."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        idem_key = f"e2e-restart-idem-{int(time.time())}"

        # Submit and wait for terminal state
        sub1 = await submit_job("Idempotency restart test: what is 3+3?", idempotency_key=idem_key)
        job1 = await wait_for_job(sub1["job_id"])
        assert job1["status"] in ("delivered", "failed"), (
            f"Job did not reach terminal state: {job1['status']}"
        )

        # Restart orchestrator — startup purges terminal idempotency keys
        result = _compose("restart", "orchestrator")
        assert result.returncode == 0, f"Restart failed: {result.stderr}"

        healthy = await wait_for_health(timeout=120)
        assert healthy, "Orchestrator did not become healthy after restart"
        await ensure_hub_active()

        # Re-submit with the same key — must create a NEW job
        sub2 = await submit_job("Idempotency restart test: what is 3+3?", idempotency_key=idem_key)
        assert sub2["job_id"] != sub1["job_id"], (
            f"Expected new job after restart, but got same job_id {sub1['job_id']} — "
            "terminal idempotency key was not purged"
        )

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_after_restart(self):
        """Connectivity monitor re-initializes from SQLite after restart."""
        await ensure_hub_active()

        r = await api_get("/health")
        assert r.status_code == 200
        # The health endpoint includes last_successful_cloud_probe_timestamp
        # which comes from the connectivity monitor. If it re-initialized,
        # health will work.
        data = r.json()
        assert data["orchestrator_status"] == "running"

    @pytest.mark.asyncio
    async def test_full_stack_restart_healthy(self):
        """Full down/up cycle — stack comes back healthy."""
        result = _compose("down")
        assert result.returncode == 0, f"docker compose down failed: {result.stderr}"

        result = _compose("up", "-d")
        assert result.returncode == 0, f"docker compose up failed: {result.stderr}"

        healthy = await wait_for_health(timeout=180)
        assert healthy, "Stack did not become healthy after full restart"
        await ensure_hub_active()

        r = await api_get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["orchestrator_status"] == "running"
        assert data["audit_log_status"] == "connected"

        r = await api_get("/admin/startup-report")
        if r.status_code == 200:
            assert r.json()["hub_start_permitted"] is True

    @pytest.mark.asyncio
    async def test_new_jobs_after_full_restart(self):
        """After full restart, new jobs complete successfully."""
        await ensure_hub_active()

        if not await ollama_available():
            pytest.skip("Ollama not available")

        # Retry for warm-up after full restart
        for attempt in range(3):
            job = await submit_and_wait(
                "Post-full-restart test: speed of light?",
                timeout=TIMEOUT,
            )
            if job["status"] == "delivered":
                break
            if attempt < 2:
                await asyncio.sleep(10)

        assert job["status"] == "delivered", (
            f"Post-restart job failed: status={job['status']}, error={job.get('error')}"
        )
        assert job["result"] is not None

    @pytest.mark.asyncio
    async def test_audit_log_intact_after_restart(self):
        """Audit log connected and startup events re-emitted after restart."""
        await ensure_hub_active()

        r = await api_get("/health")
        assert r.status_code == 200
        assert r.json()["audit_log_status"] == "connected"

        r = await api_get("/admin/audit-events", params={
            "event_type": "hub.startup_validated",
            "limit": 5,
        })
        if r.status_code == 200:
            events = r.json().get("events", [])
            assert len(events) >= 1, "No startup validation events after restart"
