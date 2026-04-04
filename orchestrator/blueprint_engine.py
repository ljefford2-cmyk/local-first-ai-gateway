"""Blueprint generation engine for Spec 6B: Sandbox Blueprint Engine.

Translates a validated RuntimeManifest into a concrete SandboxBlueprint —
the container configuration that enforces the manifest's declarations.

The engine is a hard gate: it rejects any manifest where the validation
result indicates failure. It does not attempt to "fix" invalid manifests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from manifest_validator import ManifestValidationResult
from runtime_manifest import RuntimeManifest
from sandbox_blueprint import (
    ContainerConfig,
    ContainerMount,
    ContainerNetworkConfig,
    ContainerResourceConfig,
    ContainerSecurityConfig,
    SandboxBlueprint,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# System allowlist of Linux capabilities that may be added back.
# v1: only NET_BIND_SERVICE for cloud_adapter workers.
_CAPABILITY_ALLOWLIST: dict[str, list[str]] = {
    "cloud_adapter": ["NET_BIND_SERVICE"],
}

# Secret-like environment variable name patterns (case-insensitive substrings)
_SECRET_PATTERNS = ("key", "token", "secret", "password", "credential", "auth")

DEFAULT_IMAGE = "drnt-worker:latest"
DEFAULT_BASE_DIR = "/var/drnt/workers"


class BlueprintEngine:
    """Translates validated RuntimeManifests into SandboxBlueprints.

    Args:
        base_dir: Base directory for resolving host paths. Volume host_paths
            are resolved relative to this directory.
        image: Docker image to use for worker containers.
    """

    def __init__(
        self,
        base_dir: str = DEFAULT_BASE_DIR,
        image: str = DEFAULT_IMAGE,
    ):
        self._base_dir = base_dir.rstrip("/")
        self._image = image

    def generate(
        self,
        manifest: RuntimeManifest,
        validation_result: ManifestValidationResult,
    ) -> SandboxBlueprint:
        """Generate a SandboxBlueprint from a validated manifest.

        Raises:
            ValueError: If validation_result.valid is False or is not a
                ManifestValidationResult.
        """
        # Hard gate: reject invalid manifests
        if not isinstance(validation_result, ManifestValidationResult):
            raise ValueError("validation_result must be a ManifestValidationResult")
        if not validation_result.valid:
            raise ValueError(
                f"Cannot generate blueprint for invalid manifest: "
                f"{validation_result.errors}"
            )

        blueprint_id = str(uuid.uuid4())
        created_at = _now_iso()

        return SandboxBlueprint(
            blueprint_id=blueprint_id,
            manifest_id=manifest.manifest_id,
            capability_id=manifest.capability_id,
            created_at=created_at,
            container_config=self._build_container_config(manifest, created_at),
            mounts=self._build_mounts(manifest),
            network_config=self._build_network_config(manifest),
            security_config=self._build_security_config(manifest),
            resource_config=self._build_resource_config(manifest),
        )

    def _build_container_config(
        self, manifest: RuntimeManifest, created_at: str
    ) -> ContainerConfig:
        """Build core container configuration."""
        labels = {
            "drnt.manifest_id": manifest.manifest_id,
            "drnt.capability_id": manifest.capability_id,
            "drnt.worker_type": manifest.worker_type,
            "drnt.created_at": created_at,
        }
        return ContainerConfig(
            image=self._image,
            command=[],
            working_dir="/work",
            environment={},
            labels=labels,
        )

    def _build_mounts(self, manifest: RuntimeManifest) -> list[ContainerMount]:
        """Translate VolumeMount list to ContainerMount list.

        Host paths are resolved relative to the configured base directory.
        """
        mounts = []
        for vol in manifest.volumes:
            # Resolve host path relative to base_dir
            host_path = f"{self._base_dir}{vol.host_path}"
            mounts.append(ContainerMount(
                source=host_path,
                target=vol.path,
                read_only=(vol.mode == "ro"),
            ))
        return mounts

    def _build_network_config(self, manifest: RuntimeManifest) -> ContainerNetworkConfig:
        """Determine network mode from egress_allow."""
        if manifest.network.egress_allow:
            network_mode = "drnt-egress-proxy"
        else:
            network_mode = "none"
        return ContainerNetworkConfig(
            network_mode=network_mode,
            dns=[],
            egress_allow=list(manifest.network.egress_allow),
        )

    def _build_security_config(self, manifest: RuntimeManifest) -> ContainerSecurityConfig:
        """Build security configuration with defense-in-depth hardening."""
        # Always drop ALL capabilities
        cap_drop = ["ALL"]

        # Only add back capabilities on the system allowlist for this worker_type
        allowed = _CAPABILITY_ALLOWLIST.get(manifest.worker_type, [])
        cap_add = list(allowed)

        return ContainerSecurityConfig(
            cap_drop=cap_drop,
            cap_add=cap_add,
            read_only_rootfs=True,   # Always True — defense in depth
            no_new_privileges=True,
            seccomp_profile_path="default",
            tmpfs_mounts=["/tmp:size=64m,noexec,nosuid"],
        )

    def _build_resource_config(self, manifest: RuntimeManifest) -> ContainerResourceConfig:
        """Translate manifest resource limits to container resource config."""
        return ContainerResourceConfig(
            memory_limit=f"{manifest.resources.max_memory_mb}m",
            cpu_period=100000,
            cpu_quota=manifest.resources.max_cpu_seconds * 100000,
            pids_limit=256,
            storage_opt={"size": f"{manifest.resources.max_disk_mb}m"},
        )
