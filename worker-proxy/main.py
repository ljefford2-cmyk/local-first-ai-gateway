"""Worker Proxy — Docker sidecar that exposes container management over HTTP.

This service is the sole holder of the Docker socket.  The orchestrator will
call these endpoints instead of using the Docker SDK directly.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from contextlib import asynccontextmanager

import docker
import docker.errors
import requests.exceptions
from fastapi import FastAPI, HTTPException

import registry as registry_module
from models import ContainerRunRequest, ContainerRunResponse

logging.basicConfig(
    level=os.environ.get("DRNT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("worker-proxy")

# ---------------------------------------------------------------------------
# Docker client singleton + volume resolution
# ---------------------------------------------------------------------------

_docker_client: docker.DockerClient | None = None
_host_sandbox_base: str | None = None

SANDBOX_VOLUME_NAME = os.environ.get("DRNT_SANDBOX_VOLUME", "drnt-sandbox-workdir")
SANDBOX_CONTAINER_PATH = "/var/drnt/workers"


def get_docker_client() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    return _docker_client


def resolve_host_sandbox_base() -> str | None:
    """Resolve the Docker volume's host mountpoint so the sidecar can
    translate container-internal sandbox paths to host paths."""
    global _host_sandbox_base
    if _host_sandbox_base is not None:
        return _host_sandbox_base
    try:
        vol = get_docker_client().volumes.get(SANDBOX_VOLUME_NAME)
        _host_sandbox_base = vol.attrs["Mountpoint"]
        logger.info("Resolved sandbox volume %s -> %s", SANDBOX_VOLUME_NAME, _host_sandbox_base)
        return _host_sandbox_base
    except Exception:
        logger.warning("Could not resolve host mountpoint for volume %s", SANDBOX_VOLUME_NAME)
        return None


def _translate_volumes(volumes: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Translate container-internal sandbox paths to host paths.

    The orchestrator passes volume paths as it sees them (e.g.
    /var/drnt/workers/job_worker/inbox).  Docker needs host-side paths.
    If the host sandbox base was resolved at startup, paths under
    SANDBOX_CONTAINER_PATH are rewritten to the host mountpoint.
    """
    if not _host_sandbox_base:
        return volumes
    translated: dict[str, dict[str, str]] = {}
    for src_path, config in volumes.items():
        if src_path.startswith(SANDBOX_CONTAINER_PATH + "/"):
            relative = src_path[len(SANDBOX_CONTAINER_PATH):]
            src_path = _host_sandbox_base + relative
        elif src_path == SANDBOX_CONTAINER_PATH:
            src_path = _host_sandbox_base
        translated[src_path] = config
    return translated


# ---------------------------------------------------------------------------
# Garbage collection for orphaned worker containers
# ---------------------------------------------------------------------------

GC_INTERVAL = int(os.environ.get("DRNT_GC_INTERVAL_SECONDS", "60"))
GC_MAX_AGE = int(os.environ.get("DRNT_GC_MAX_AGE_SECONDS", "300"))

_gc_task: asyncio.Task | None = None


def _gc_sweep() -> int:
    """Remove stopped worker containers older than GC_MAX_AGE seconds.

    Returns the number of containers removed.  Never raises — all Docker
    errors are caught and logged so the sidecar keeps running.
    """
    removed = 0
    try:
        client = get_docker_client()
        containers = client.containers.list(
            all=True,
            filters={"label": "drnt.role=worker", "status": "exited"},
        )
    except Exception:
        logger.warning("GC: failed to list containers", exc_info=True)
        return 0

    now = time.time()
    for ctr in containers:
        try:
            finished_at = ctr.attrs.get("State", {}).get("FinishedAt", "")
            if not finished_at:
                continue
            # Docker timestamps are ISO 8601 with nanosecond precision.
            # time.strptime doesn't handle nanos — truncate to seconds.
            ts_str = finished_at.split(".")[0].replace("T", " ").replace("Z", "")
            finished_ts = datetime.datetime.strptime(
                ts_str, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=datetime.timezone.utc).timestamp()
            age = now - finished_ts
            if age >= GC_MAX_AGE:
                ctr.remove(force=True)
                logger.info("GC: removed container %s (age=%.0fs)", ctr.id[:12], age)
                removed += 1
        except Exception:
            logger.warning("GC: error processing container %s", ctr.id[:12], exc_info=True)
    return removed


async def _gc_loop() -> None:
    """Background loop that periodically sweeps orphaned containers."""
    logger.info("GC: started (interval=%ds, max_age=%ds)", GC_INTERVAL, GC_MAX_AGE)
    while True:
        await asyncio.sleep(GC_INTERVAL)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _gc_sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("GC: sweep failed", exc_info=True)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gc_task
    # Load the default-deny registry before accepting any requests. A missing
    # or malformed registry is a hard startup failure — the proxy must not
    # accept traffic without a known allowlist.
    registry_module.set_active(registry_module.load_registry())
    resolve_host_sandbox_base()
    _gc_task = asyncio.create_task(_gc_loop())
    logger.info("Worker proxy ready")
    yield
    if _gc_task is not None:
        _gc_task.cancel()
        try:
            await _gc_task
        except asyncio.CancelledError:
            pass
        logger.info("GC: stopped")
    if _docker_client is not None:
        _docker_client.close()
        logger.info("Docker client closed")


app = FastAPI(title="DRNT Worker Proxy", lifespan=lifespan)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    try:
        get_docker_client().ping()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# GET /images/{name}
# ---------------------------------------------------------------------------

@app.get("/images/{name:path}")
async def check_image(name: str):
    logger.info("Checking image: %s", name)
    try:
        get_docker_client().images.get(name)
        return {"exists": True}
    except docker.errors.ImageNotFound:
        return {"exists": False}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# POST /containers/run
# ---------------------------------------------------------------------------

@app.post("/containers/run", response_model=ContainerRunResponse)
async def run_container(req: ContainerRunRequest):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run_container_sync, req)


def _run_container_sync(req: ContainerRunRequest) -> ContainerRunResponse:
    """Blocking container lifecycle: create -> start -> wait -> remove."""
    client = get_docker_client()
    container = None
    container_id: str | None = None

    try:
        logger.info("Creating container %s (image=%s, timeout=%ds)",
                     req.name, req.image, req.wall_timeout)

        create_kwargs: dict = dict(
            image=req.image,
            name=req.name,
            labels=req.labels,
            environment=req.environment,
            volumes=_translate_volumes(req.volumes),
            cap_drop=req.cap_drop,
            read_only=req.read_only,
            security_opt=req.security_opt,
            mem_limit=req.mem_limit,
            pids_limit=req.pids_limit,
            tmpfs=req.tmpfs,
            detach=True,
        )

        if req.network_mode:
            create_kwargs["network_mode"] = req.network_mode
        elif req.network:
            create_kwargs["network"] = req.network

        container = client.containers.create(**create_kwargs)
        container_id = container.id
        logger.info("Container created: %s (%s)", req.name, container_id[:12])

        container.start()
        logger.info("Container started: %s", container_id[:12])

        try:
            exit_info = container.wait(timeout=req.wall_timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            logger.warning("Container %s timed out after %ds", container_id[:12], req.wall_timeout)
            try:
                container.kill()
            except Exception:
                pass
            return ContainerRunResponse(
                container_id=container_id,
                exit_code=-1,
                status="timeout",
            )

        exit_code = exit_info.get("StatusCode", -1)
        logger.info("Container %s exited: code=%d", container_id[:12], exit_code)

        if exit_code != 0:
            logs = container.logs(tail=200).decode("utf-8", errors="replace")
            return ContainerRunResponse(
                container_id=container_id,
                exit_code=exit_code,
                status="failed",
                logs=logs,
            )

        return ContainerRunResponse(
            container_id=container_id,
            exit_code=exit_code,
            status="completed",
        )

    except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
        # Timeout during wait is handled above; this catches create/start timeouts
        logger.warning("Timeout during container lifecycle for %s", req.name)
        return ContainerRunResponse(
            container_id=container_id,
            exit_code=-1,
            status="timeout",
        )
    except docker.errors.ImageNotFound:
        logger.error("Image not found: %s", req.image)
        raise HTTPException(status_code=404, detail=f"Image not found: {req.image}")
    except docker.errors.APIError as exc:
        logger.error("Docker API error for %s: %s", req.name, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.error("Unexpected error for %s: %s", req.name, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if container is not None:
            try:
                container.remove(force=True)
                logger.info("Container removed: %s", container_id[:12] if container_id else "?")
            except Exception:
                logger.warning("Failed to remove container %s",
                               container_id[:12] if container_id else "?")


# ---------------------------------------------------------------------------
# GET /containers/{id}/logs
# ---------------------------------------------------------------------------

@app.get("/containers/{container_id}/logs")
async def get_container_logs(container_id: str, tail: int = 200):
    logger.info("Fetching logs for container %s (tail=%d)", container_id[:12], tail)
    try:
        container = get_docker_client().containers.get(container_id)
        logs = container.logs(tail=tail).decode("utf-8", errors="replace")
        return {"container_id": container_id, "logs": logs}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Container {container_id} not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# DELETE /containers/{id}
# ---------------------------------------------------------------------------

@app.delete("/containers/{container_id}")
async def remove_container(container_id: str):
    logger.info("Force-removing container %s", container_id[:12])
    try:
        container = get_docker_client().containers.get(container_id)
        container.remove(force=True)
        logger.info("Container %s removed", container_id[:12])
        return {"status": "removed", "container_id": container_id}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Container {container_id} not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9100, timeout_keep_alive=360)
