"""Pydantic models for the worker-proxy HTTP API."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ContainerRunRequest(BaseModel):
    """Full container configuration — mirrors what WorkerExecutor._execute_sync()
    passes to client.containers.create()."""

    image: str
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    # Docker SDK volume format: {"/host/path": {"bind": "/ctr/path", "mode": "ro"}}
    volumes: dict[str, dict[str, str]] = Field(default_factory=dict)
    security_opt: list[str] = Field(default_factory=list)
    mem_limit: str = "256m"
    pids_limit: int = 64
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    read_only: bool = True
    tmpfs: dict[str, str] = Field(default_factory=dict)
    network_mode: Optional[str] = None
    network: Optional[str] = None
    wall_timeout: int = 300


class ContainerRunResponse(BaseModel):
    """Result of a container run lifecycle."""

    container_id: Optional[str] = None
    exit_code: Optional[int] = None
    status: str  # "completed" | "failed" | "timeout"
    logs: Optional[str] = None
