"""Worker Executor — runs tasks in one-shot sandboxed Docker containers.

Uses the Docker SDK to create short-lived containers that read a task from
an inbox volume mount and write results to an outbox volume mount.  The
container image is expected to run worker/worker_agent.py (or equivalent).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import docker
import docker.errors

logger = logging.getLogger(__name__)


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
    """Creates and manages one-shot worker containers via the Docker SDK."""

    def __init__(
        self,
        sandbox_base_dir: str = "/var/drnt/workers",
        docker_url: str = "unix:///var/run/docker.sock",
        ollama_url: str = "http://ollama:11434",
    ) -> None:
        self.sandbox_base_dir = Path(sandbox_base_dir)
        self.docker_url = docker_url
        self.ollama_url = ollama_url
        self._client: Optional[docker.DockerClient] = None

    # -- Docker client ------------------------------------------------------

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(base_url=self.docker_url)
        return self._client

    # -- Sandbox directory management ---------------------------------------

    def _prepare_sandbox_dir(self, job_id: str, worker_id: str) -> Path:
        sandbox = self.sandbox_base_dir / f"{job_id}_{worker_id}"
        inbox = sandbox / "inbox"
        outbox = sandbox / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
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

    # -- Synchronous container lifecycle ------------------------------------

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
        client = self._get_client()
        sandbox_dir = self._prepare_sandbox_dir(job_id, worker_id)
        container = None
        container_id: Optional[str] = None
        start_ms = int(time.monotonic() * 1000)

        try:
            self._write_task(sandbox_dir, task_payload)

            container_name = f"drnt-worker-{job_id[:12]}-{worker_id[:8]}"

            inbox_path = str(sandbox_dir / "inbox")
            outbox_path = str(sandbox_dir / "outbox")

            # Security: build security_options and tmpfs from config
            mem_limit = security_config.get("mem_limit", "256m")
            pids_limit = security_config.get("pids_limit", 64)
            tmpfs_mounts = security_config.get("tmpfs", {"/tmp": "size=64m,noexec"})

            container = client.containers.create(
                image=image,
                name=container_name,
                labels={
                    "drnt.worker_id": worker_id,
                    "drnt.job_id": job_id,
                    "drnt.capability_id": capability_id,
                    "drnt.role": "worker",
                },
                environment={
                    "OLLAMA_URL": self.ollama_url,
                    "DRNT_WORKER_ID": worker_id,
                    "DRNT_JOB_ID": job_id,
                    "DRNT_CAPABILITY": capability_id,
                },
                volumes={
                    inbox_path: {"bind": "/inbox", "mode": "ro"},
                    outbox_path: {"bind": "/outbox", "mode": "rw"},
                },
                cap_drop=["ALL"],
                read_only=True,
                security_opt=["no-new-privileges"],
                mem_limit=mem_limit,
                pids_limit=pids_limit,
                tmpfs=tmpfs_mounts,
                network="drnt-internal",
                detach=True,
            )
            container_id = container.id

            container.start()
            exit_info = container.wait(timeout=wall_timeout)
            exit_code = exit_info.get("StatusCode", -1)

            latency_ms = int(time.monotonic() * 1000) - start_ms

            try:
                result_data = self._read_result(sandbox_dir)
            except WorkerExecutionError:
                # Container ran but produced no result file
                logs = container.logs(tail=200).decode("utf-8", errors="replace")
                return WorkerResult(
                    success=False,
                    response_text="",
                    token_count_in=0,
                    token_count_out=0,
                    latency_ms=latency_ms,
                    model="",
                    error=f"No result.json produced. exit_code={exit_code}, logs: {logs}",
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

        except docker.errors.ContainerError as exc:
            latency_ms = int(time.monotonic() * 1000) - start_ms
            return WorkerResult(
                success=False,
                response_text="",
                token_count_in=0,
                token_count_out=0,
                latency_ms=latency_ms,
                model="",
                error=f"Container error: {exc}",
                exit_code=exc.exit_status,
                container_id=container_id,
            )
        except Exception as exc:
            latency_ms = int(time.monotonic() * 1000) - start_ms
            raise WorkerExecutionError(
                f"Failed to execute worker container: {exc}"
            ) from exc
        finally:
            # Clean up container and sandbox
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    logger.warning("Failed to remove container %s", container_id)
            self._cleanup_sandbox_dir(sandbox_dir)

    # -- Health checks ------------------------------------------------------

    def check_docker_available(self) -> bool:
        try:
            self._get_client().ping()
            return True
        except Exception:
            return False

    def check_image_exists(self, image: str) -> bool:
        try:
            self._get_client().images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False
        except Exception:
            return False
