"""Worker Executor — runs tasks in one-shot sandboxed Docker containers.

Uses the worker-proxy sidecar HTTP API to manage container lifecycle.
The orchestrator never accesses the Docker socket directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_PROXY_URL = "http://worker-proxy:9100"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class WorkerResult:
    success: bool
    response_text: str
    token_count_in: int
    token_count_out: int
    latency_ms: int
    model: str
    error: Optional[str] = None
    exit_code: int = 0
    container_id: Optional[str] = None


class WorkerExecutionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class WorkerExecutor:
    """Creates and manages one-shot worker containers via the worker-proxy sidecar."""

    def __init__(
        self,
        sandbox_base_dir: str = "/var/drnt/workers",
        ollama_url: str = "http://ollama:11434",
        worker_proxy_url: Optional[str] = None,
    ) -> None:
        self.sandbox_base_dir = Path(sandbox_base_dir)
        self.ollama_url = ollama_url
        self.worker_proxy_url = (
            worker_proxy_url
            or os.environ.get("DRNT_WORKER_PROXY_URL", _DEFAULT_PROXY_URL)
        )
        self._max_concurrent_workers = int(
            os.environ.get("DRNT_MAX_CONCURRENT_WORKERS", "4")
        )
        self._semaphore = asyncio.Semaphore(self._max_concurrent_workers)
        self._semaphore_timeout = 30  # seconds

    # -- Sandbox directory management ---------------------------------------

    def _prepare_sandbox_dir(self, job_id: str, worker_id: str) -> Path:
        sandbox = self.sandbox_base_dir / f"{job_id}_{worker_id}"
        inbox = sandbox / "inbox"
        outbox = sandbox / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        # Worker runs as non-root (drnt user); outbox must be writable.
        os.chmod(outbox, 0o777)
        return sandbox

    def _write_task(self, sandbox_dir: Path, task_payload: dict[str, Any]) -> None:
        task_file = sandbox_dir / "inbox" / "task.json"
        with open(task_file, "w") as f:
            json.dump(task_payload, f, indent=2)

    def _read_result(self, sandbox_dir: Path) -> dict[str, Any]:
        result_file = sandbox_dir / "outbox" / "result.json"
        if not result_file.exists():
            raise WorkerExecutionError(
                f"result.json not found in {result_file}"
            )
        with open(result_file, "r") as f:
            return json.load(f)

    def _cleanup_sandbox_dir(self, sandbox_dir: Path) -> None:
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    # -- Public async entry point -------------------------------------------

    async def execute(
        self,
        worker_id: str,
        job_id: str,
        capability_id: str,
        image: str,
        task_payload: dict[str, Any],
        resource_config: dict[str, Any],
        security_config: dict[str, Any],
        wall_timeout: int = 300,
    ) -> WorkerResult:
        logger.info(
            "Job %s waiting for worker slot (max=%d)",
            job_id, self._max_concurrent_workers,
        )
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self._semaphore_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Job %s timed out waiting for worker slot after %ds",
                job_id, self._semaphore_timeout,
            )
            return WorkerResult(
                success=False,
                response_text="",
                token_count_in=0,
                token_count_out=0,
                latency_ms=0,
                model="",
                error="max concurrent workers reached",
            )

        logger.info("Job %s acquired worker slot", job_id)
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                self._execute_sync,
                worker_id,
                job_id,
                capability_id,
                image,
                task_payload,
                resource_config,
                security_config,
                wall_timeout,
            )
        finally:
            self._semaphore.release()

    # -- Synchronous container lifecycle (via sidecar) ----------------------

    def _execute_sync(
        self,
        worker_id: str,
        job_id: str,
        capability_id: str,
        image: str,
        task_payload: dict[str, Any],
        resource_config: dict[str, Any],
        security_config: dict[str, Any],
        wall_timeout: int,
    ) -> WorkerResult:
        sandbox_dir = self._prepare_sandbox_dir(job_id, worker_id)
        start_ms = int(time.monotonic() * 1000)

        try:
            self._write_task(sandbox_dir, task_payload)

            container_name = f"drnt-worker-{job_id[:12]}-{worker_id[:8]}"

            # Volume paths — container-internal paths backed by the shared volume.
            # The sidecar resolves these to host paths for Docker.
            inbox_path = str(sandbox_dir / "inbox")
            outbox_path = str(sandbox_dir / "outbox")

            # Security options
            mem_limit = security_config.get("mem_limit", "256m")
            pids_limit = security_config.get("pids_limit", 64)
            tmpfs_mounts = security_config.get("tmpfs", {"/tmp": "size=64m,noexec"})

            seccomp_profile_path = security_config.get("seccomp_profile")
            security_opt_list = ["no-new-privileges"]
            if seccomp_profile_path:
                security_opt_list.append(f"seccomp={seccomp_profile_path}")

            network_mode_cfg = security_config.get("network_mode", "none")

            # Build the sidecar request body (mirrors ContainerRunRequest schema)
            request_body: dict[str, Any] = {
                "image": image,
                "name": container_name,
                "labels": {
                    "drnt.worker_id": worker_id,
                    "drnt.job_id": job_id,
                    "drnt.capability_id": capability_id,
                    "drnt.role": "worker",
                },
                "environment": {
                    "OLLAMA_URL": self.ollama_url,
                    "DRNT_WORKER_ID": worker_id,
                    "DRNT_JOB_ID": job_id,
                    "DRNT_CAPABILITY": capability_id,
                },
                "volumes": {
                    inbox_path: {"bind": "/inbox", "mode": "ro"},
                    outbox_path: {"bind": "/outbox", "mode": "rw"},
                },
                "cap_drop": ["ALL"],
                "read_only": True,
                "security_opt": security_opt_list,
                "mem_limit": mem_limit,
                "pids_limit": pids_limit,
                "tmpfs": tmpfs_mounts,
                "wall_timeout": wall_timeout,
            }

            if network_mode_cfg == "none":
                request_body["network_mode"] = "none"
            else:
                request_body["network"] = network_mode_cfg

            # POST to sidecar — timeout covers container lifecycle + overhead
            http_timeout = wall_timeout + 30
            with httpx.Client(timeout=http_timeout) as client:
                resp = client.post(
                    f"{self.worker_proxy_url}/containers/run",
                    json=request_body,
                )

            if resp.status_code >= 500:
                raise WorkerExecutionError(
                    f"Sidecar error {resp.status_code}: {resp.text}"
                )
            if resp.status_code >= 400:
                raise WorkerExecutionError(
                    f"Sidecar request error {resp.status_code}: {resp.text}"
                )

            sidecar_result = resp.json()
            container_id = sidecar_result.get("container_id")
            status = sidecar_result.get("status")
            exit_code = sidecar_result.get("exit_code", -1)
            latency_ms = int(time.monotonic() * 1000) - start_ms

            if status == "timeout":
                return WorkerResult(
                    success=False,
                    response_text="",
                    token_count_in=0,
                    token_count_out=0,
                    latency_ms=latency_ms,
                    model="",
                    error=f"Container timed out after {wall_timeout}s",
                    exit_code=-1,
                    container_id=container_id,
                )

            if status == "failed":
                logs = sidecar_result.get("logs", "")
                return WorkerResult(
                    success=False,
                    response_text="",
                    token_count_in=0,
                    token_count_out=0,
                    latency_ms=latency_ms,
                    model="",
                    error=f"Container failed. exit_code={exit_code}, logs: {logs}",
                    exit_code=exit_code,
                    container_id=container_id,
                )

            # status == "completed"
            try:
                result_data = self._read_result(sandbox_dir)
            except WorkerExecutionError:
                return WorkerResult(
                    success=False,
                    response_text="",
                    token_count_in=0,
                    token_count_out=0,
                    latency_ms=latency_ms,
                    model="",
                    error=f"No result.json produced. exit_code={exit_code}",
                    exit_code=exit_code,
                    container_id=container_id,
                )

            success = result_data.get("status") == "success" and exit_code == 0

            return WorkerResult(
                success=success,
                response_text=result_data.get("result", ""),
                token_count_in=result_data.get("token_count_in", 0),
                token_count_out=result_data.get("token_count_out", 0),
                latency_ms=latency_ms,
                model=result_data.get("model", ""),
                error=result_data.get("error"),
                exit_code=exit_code,
                container_id=container_id,
            )

        except WorkerExecutionError:
            raise
        except httpx.ConnectError as exc:
            raise WorkerExecutionError(
                f"Worker proxy unreachable at {self.worker_proxy_url}: {exc}"
            ) from exc
        except Exception as exc:
            raise WorkerExecutionError(
                f"Failed to execute worker container: {exc}"
            ) from exc
        finally:
            self._cleanup_sandbox_dir(sandbox_dir)

    # -- Health checks ------------------------------------------------------

    def check_docker_available(self) -> bool:
        """Check that the worker-proxy sidecar is reachable."""
        try:
            resp = httpx.get(f"{self.worker_proxy_url}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    def check_image_exists(self, image: str) -> bool:
        """Check that a Docker image exists via the worker-proxy sidecar."""
        try:
            resp = httpx.get(
                f"{self.worker_proxy_url}/images/{image}", timeout=10.0
            )
            if resp.status_code != 200:
                return False
            return resp.json().get("exists", False)
        except Exception:
            return False
