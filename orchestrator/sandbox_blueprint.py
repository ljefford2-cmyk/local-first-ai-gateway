"""Sandbox blueprint schema for Spec 6B: Sandbox Blueprint Engine.

The SandboxBlueprint is the container configuration that enforces a validated
RuntimeManifest's declarations. It is the bridge between "what the worker
declares" and "what the container runtime enforces."

Phase 6B does NOT call Docker. It produces a configuration object that
Phase 6E will use to actually create containers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class ContainerMount:
    """A resolved volume mount for the container."""
    source: str       # Host path
    target: str       # Container path
    read_only: bool   # From manifest mode


@dataclass
class ContainerNetworkConfig:
    """Network configuration for the container."""
    network_mode: str              # "none" | "drnt-egress-proxy"
    dns: list[str] = field(default_factory=list)  # Empty — workers don't resolve DNS
    egress_allow: list[str] = field(default_factory=list)  # Allowlisted endpoints from manifest


@dataclass
class ContainerSecurityConfig:
    """Security configuration for the container."""
    cap_drop: list[str] = field(default_factory=lambda: ["ALL"])
    cap_add: list[str] = field(default_factory=list)
    read_only_rootfs: bool = True
    no_new_privileges: bool = True
    seccomp_profile_path: str = "default"
    tmpfs_mounts: list[str] = field(default_factory=lambda: ["/tmp:size=64m,noexec,nosuid"])


@dataclass
class ContainerResourceConfig:
    """Resource limits for the container."""
    memory_limit: str       # "{max_memory_mb}m"
    cpu_period: int = 100000
    cpu_quota: int = 0      # Computed from max_cpu_seconds
    pids_limit: int = 256
    storage_opt: dict = field(default_factory=dict)  # {"size": "{max_disk_mb}m"}


@dataclass
class ContainerConfig:
    """Core container configuration."""
    image: str                          # Base image
    command: list[str] = field(default_factory=list)  # Entrypoint command
    working_dir: str = "/work"
    environment: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxBlueprint:
    """Container configuration that enforces a validated RuntimeManifest.

    Produced by BlueprintEngine.generate(). Consumed by Phase 6E to
    actually create Docker containers.
    """
    blueprint_id: str
    manifest_id: str
    capability_id: str
    created_at: str
    container_config: ContainerConfig
    mounts: list[ContainerMount]
    network_config: ContainerNetworkConfig
    security_config: ContainerSecurityConfig
    resource_config: ContainerResourceConfig
