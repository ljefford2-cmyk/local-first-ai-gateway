"""Pydantic models for the worker-proxy HTTP API.

Enforces a default-deny allowlist at request validation time. Permissive
values (read_only=False, cap_drop=[], network_mode="host", off-sandbox
bind mounts, unregistered images, resource limits above registry caps,
labels missing the worker marker) are rejected with 422 before the
request reaches the Docker socket.

Deferred fields — intentionally not exposed on this model to minimize the
attack surface at the HTTP boundary. extra="forbid" ensures any JSON
attempting to smuggle them in is rejected:
    cap_add          capability additions always rejected in Phase 2;
                     v0.3+ may reintroduce for specific worker types
                     through blueprint construction
    cpu_period,
    cpu_quota        CPU throttling not yet wired from blueprint
    storage_opt      disk quota not yet wired from blueprint
    mounts           only bind-via-volumes-dict supported
    command          worker image entrypoint is authoritative
    working_dir      fixed by the worker image
See Phase 2B Step 1 trace for rationale.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from registry import get_active, parse_mem_bytes


REQUIRED_WORKER_LABEL_KEY = "drnt.role"
REQUIRED_WORKER_LABEL_VALUE = "worker"
REQUIRED_NO_NEW_PRIVS = "no-new-privileges"
SANDBOX_BASE_PATH = "/var/drnt/workers/"


class ContainerRunRequest(BaseModel):
    """Full container configuration — mirrors what WorkerExecutor._execute_sync()
    passes to client.containers.create()."""

    model_config = ConfigDict(extra="forbid")

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

    @field_validator("cap_drop")
    @classmethod
    def _cap_drop_contains_all(cls, v: list[str]) -> list[str]:
        if "ALL" not in v:
            raise ValueError("cap_drop must contain 'ALL' (default-deny)")
        return v

    @field_validator("read_only")
    @classmethod
    def _read_only_true(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("read_only must be True")
        return v

    @field_validator("security_opt")
    @classmethod
    def _security_opt_no_new_privs(cls, v: list[str]) -> list[str]:
        if REQUIRED_NO_NEW_PRIVS not in v:
            raise ValueError(
                f"security_opt must contain '{REQUIRED_NO_NEW_PRIVS}'"
            )
        return v

    @field_validator("network_mode")
    @classmethod
    def _network_mode_none_or_unset(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "none":
            raise ValueError(
                f"network_mode must be 'none' or unset, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _enforce_registry_allowlist(self) -> "ContainerRunRequest":
        reg = get_active()

        if self.image not in reg.approved_images:
            raise ValueError(
                f"image {self.image!r} not in approved_images registry"
            )

        # Network: when network_mode is unset, network must be set to an
        # allowlisted name. When network_mode="none", network must not be
        # set (Docker ignores one or the other; we forbid ambiguity).
        if self.network_mode is None:
            if self.network is None:
                raise ValueError(
                    "either network_mode='none' or network must be set"
                )
            if self.network not in reg.allowed_networks:
                raise ValueError(
                    f"network {self.network!r} not in allowed_networks "
                    f"registry"
                )
        else:
            # network_mode == "none"
            if self.network is not None:
                raise ValueError(
                    "network must be unset when network_mode='none'"
                )

        # Volume source paths: either under the sandbox base or a named
        # volume in the allowlist. Reject docker.sock and any other bind.
        for src in self.volumes:
            if src.startswith(SANDBOX_BASE_PATH):
                continue
            if src in reg.allowed_volume_names:
                continue
            raise ValueError(
                f"volume source {src!r} not under {SANDBOX_BASE_PATH} "
                f"and not in allowed_volume_names"
            )

        try:
            mem_bytes = parse_mem_bytes(self.mem_limit)
            cap_mem_bytes = parse_mem_bytes(reg.caps.mem_limit_max)
        except ValueError as exc:
            raise ValueError(f"mem_limit parse error: {exc}") from exc
        if mem_bytes > cap_mem_bytes:
            raise ValueError(
                f"mem_limit {self.mem_limit!r} exceeds cap "
                f"{reg.caps.mem_limit_max!r}"
            )

        if self.pids_limit > reg.caps.pids_limit_max:
            raise ValueError(
                f"pids_limit {self.pids_limit} exceeds cap "
                f"{reg.caps.pids_limit_max}"
            )

        if self.wall_timeout > reg.caps.wall_timeout_max:
            raise ValueError(
                f"wall_timeout {self.wall_timeout} exceeds cap "
                f"{reg.caps.wall_timeout_max}"
            )

        actual_marker = self.labels.get(REQUIRED_WORKER_LABEL_KEY)
        if actual_marker != REQUIRED_WORKER_LABEL_VALUE:
            raise ValueError(
                f"labels must contain {REQUIRED_WORKER_LABEL_KEY}="
                f"{REQUIRED_WORKER_LABEL_VALUE!r} (got {actual_marker!r})"
            )

        return self


class ContainerRunResponse(BaseModel):
    """Result of a container run lifecycle."""

    container_id: Optional[str] = None
    exit_code: Optional[int] = None
    status: str  # "completed" | "failed" | "timeout"
    logs: Optional[str] = None
