"""Runtime manifest schema for Spec 6A: Silo Runtime Security.

Declares what a worker is permitted to do at runtime. Every worker agent must
present a RuntimeManifest before execution. The manifest is a declarative
permission contract — the sandbox (Phase 6B) enforces these declarations.

V1 Simplifications:
- No Docker API calls. The manifest is a data structure + validator only.
- Seccomp profile is a string reference, not actual JSON.
- No runtime enforcement. Validation is a gate check; enforcement is Phase 6B.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# Allowed base paths for worker volume mounts
ALLOWED_VOLUME_BASE_PATHS = frozenset({"/work", "/inbox", "/outbox", "/skills", "/tmp"})

# Forbidden mount paths — host root and sensitive system directories
FORBIDDEN_VOLUME_PATHS = frozenset({"/", "/etc", "/proc", "/sys", "/dev", "/boot", "/root", "/home"})

# Valid worker types
VALID_WORKER_TYPES = frozenset({"ollama_local", "cloud_adapter", "tool_executor"})

# Valid volume mount modes
VALID_VOLUME_MODES = frozenset({"ro", "rw"})

# Valid ingress modes
VALID_INGRESS_MODES = frozenset({"none", "hub_only"})

# Valid seccomp profile values
VALID_SECCOMP_PROFILES = frozenset({"default", "strict"})

# Non-negotiable Linux capabilities that must always be dropped
REQUIRED_DROPPED_CAPABILITIES = frozenset({"NET_RAW", "SYS_ADMIN", "SYS_PTRACE", "MKNOD"})

# Minimum WAL level required per worker type
WORKER_TYPE_MIN_WAL = {
    "ollama_local": 0,
    "cloud_adapter": 0,
    "tool_executor": 1,
}

# Default resource ceilings (configurable via SystemResourceCeilings)
DEFAULT_MAX_MEMORY_MB = 512
DEFAULT_MAX_WALL_SECONDS = 300

# Audit log path — workers must not have rw access here
AUDIT_LOG_PATH = "/var/drnt/audit"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class VolumeMount:
    """A declared volume mount inside the worker sandbox."""
    path: str          # Mount path inside worker (e.g., "/work", "/inbox")
    mode: str          # "ro" | "rw"
    host_path: str     # Actual host path (resolved at sandbox creation)


@dataclass
class NetworkPolicy:
    """Network permission declarations for a worker."""
    egress_allow: list[str] = field(default_factory=list)   # Allowlisted endpoints
    egress_deny_all: bool = True                            # Default True — only egress_allow reachable
    ingress: str = "none"                                   # "none" | "hub_only"


@dataclass
class ResourceLimits:
    """Resource ceilings for a worker."""
    max_memory_mb: int = 256
    max_cpu_seconds: int = 60
    max_wall_seconds: int = 120
    max_disk_mb: int = 100


@dataclass
class SecurityPolicy:
    """Security constraints for a worker sandbox."""
    drop_capabilities: list[str] = field(default_factory=lambda: [
        "ALL",
    ])
    read_only_root: bool = True
    no_new_privileges: bool = True
    seccomp_profile: str = "default"


@dataclass
class SystemResourceCeilings:
    """Configurable system-level ceilings for manifest validation."""
    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB
    max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS


@dataclass
class RuntimeManifest:
    """Declarative permission contract for a worker agent.

    Every worker must present this manifest before execution. The manifest
    binds to a governing capability from the Spec 2 registry and declares
    filesystem, network, resource, and security permissions.
    """
    capability_id: str
    worker_type: str
    volumes: list[VolumeMount] = field(default_factory=list)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    manifest_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=_now_iso)
