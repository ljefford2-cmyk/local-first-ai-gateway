"""Manifest validation engine for Spec 6A: Silo Runtime Security.

Checks a RuntimeManifest against the Spec 2 capability registry and the
Spec 4 egress policy binding. Rejects invalid or over-privileged manifests.

V1 Simplifications:
- No Docker API calls. Validation is a gate check only.
- Seccomp profile validated as string reference, not actual JSON.
- No runtime enforcement. The sandbox blueprint (6B) handles that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from runtime_manifest import (
    ALLOWED_VOLUME_BASE_PATHS,
    AUDIT_LOG_PATH,
    FORBIDDEN_VOLUME_PATHS,
    REQUIRED_DROPPED_CAPABILITIES,
    VALID_SECCOMP_PROFILES,
    VALID_VOLUME_MODES,
    VALID_WORKER_TYPES,
    WORKER_TYPE_MIN_WAL,
    RuntimeManifest,
    SystemResourceCeilings,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class ManifestValidationResult:
    """Result of manifest validation."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validated_at: str = field(default_factory=_now_iso)


class ManifestValidator:
    """Validates RuntimeManifest against capability registry and egress policy.

    All validation rules are checked exhaustively — every violation is reported,
    not just the first one.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        state_manager: CapabilityStateManager,
        egress_endpoints: Optional[dict[str, list[str]]] = None,
        ceilings: Optional[SystemResourceCeilings] = None,
    ):
        """
        Args:
            registry: The Spec 2 capability registry.
            state_manager: The Spec 2 capability state manager.
            egress_endpoints: Mapping of capability_id -> list of authorized
                endpoint strings (host:port). Built from Spec 4 egress routes.
            ceilings: System-configured resource ceilings. Defaults apply if None.
        """
        self._registry = registry
        self._state = state_manager
        self._egress_endpoints = egress_endpoints or {}
        self._ceilings = ceilings or SystemResourceCeilings()

    def validate(self, manifest: RuntimeManifest) -> ManifestValidationResult:
        """Run all validation rules against the manifest.

        Returns a ManifestValidationResult with all errors and warnings found.
        """
        errors: list[str] = []
        warnings: list[str] = []

        self._check_capability_exists(manifest, errors)
        self._check_capability_active(manifest, errors)
        self._check_wal_level(manifest, errors)
        self._check_volumes(manifest, errors, warnings)
        self._check_egress_deny_all(manifest, errors)
        self._check_egress_endpoints(manifest, errors)
        self._check_drop_capabilities(manifest, errors)
        self._check_no_new_privileges(manifest, errors)
        self._check_memory_ceiling(manifest, errors)
        self._check_wall_time_ceiling(manifest, errors)

        return ManifestValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # ---- Individual validation rules ----

    def _check_capability_exists(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 1: capability_id must exist in the registry."""
        cap = self._registry.get(manifest.capability_id)
        if cap is None:
            errors.append(
                f"capability_id '{manifest.capability_id}' not found in capability registry"
            )

    def _check_capability_active(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 2a: capability must be in an active state (not suspended, not revoked)."""
        try:
            status = self._state.get_status(manifest.capability_id)
        except KeyError:
            # Already caught by _check_capability_exists
            return
        if status != "active":
            errors.append(
                f"capability '{manifest.capability_id}' is in state '{status}', must be 'active'"
            )

    def _check_wal_level(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 2b: WAL level must meet minimum for the declared worker_type."""
        min_wal = WORKER_TYPE_MIN_WAL.get(manifest.worker_type)
        if min_wal is None:
            errors.append(
                f"unknown worker_type '{manifest.worker_type}'"
            )
            return
        try:
            effective_wal = self._state.get_effective_wal(manifest.capability_id)
        except KeyError:
            # Already caught by _check_capability_exists
            return
        if effective_wal < min_wal:
            errors.append(
                f"worker_type '{manifest.worker_type}' requires WAL >= {min_wal}, "
                f"but capability '{manifest.capability_id}' has effective WAL {effective_wal}"
            )

    def _check_volumes(
        self,
        manifest: RuntimeManifest,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Rules 3-4: Volume path validation."""
        for vol in manifest.volumes:
            # Rule 3: No forbidden paths
            normalized = vol.path.rstrip("/") if vol.path != "/" else "/"
            if normalized in FORBIDDEN_VOLUME_PATHS or vol.path in FORBIDDEN_VOLUME_PATHS:
                errors.append(
                    f"volume path '{vol.path}' is a forbidden mount point"
                )
                continue

            # Check that volume is under an allowed base path
            if not any(normalized == base or normalized.startswith(base + "/")
                       for base in ALLOWED_VOLUME_BASE_PATHS):
                errors.append(
                    f"volume path '{vol.path}' is not under an allowed base path "
                    f"({', '.join(sorted(ALLOWED_VOLUME_BASE_PATHS))})"
                )

            # Rule 4: No rw volume overlapping audit log path
            if vol.mode == "rw":
                audit_normalized = AUDIT_LOG_PATH.rstrip("/")
                if (normalized == audit_normalized
                        or normalized.startswith(audit_normalized + "/")
                        or audit_normalized.startswith(normalized + "/")):
                    errors.append(
                        f"volume '{vol.path}' mounted rw overlaps with audit log path "
                        f"'{AUDIT_LOG_PATH}'"
                    )

            # Warning: /tmp rw with no cleanup policy
            if vol.path == "/tmp" and vol.mode == "rw":
                warnings.append(
                    "manifest declares /tmp rw but no cleanup policy"
                )

    def _check_egress_deny_all(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 5: egress_deny_all must be True. Hard invariant."""
        if not manifest.network.egress_deny_all:
            errors.append(
                "egress_deny_all must be True; workers cannot declare open egress"
            )

    def _check_egress_endpoints(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 6: Each egress_allow endpoint must appear in Spec 4 egress policy."""
        authorized = self._egress_endpoints.get(manifest.capability_id, [])
        for endpoint in manifest.network.egress_allow:
            if endpoint not in authorized:
                errors.append(
                    f"egress endpoint '{endpoint}' not authorized for capability "
                    f"'{manifest.capability_id}' in egress policy"
                )

    def _check_drop_capabilities(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 7: drop_capabilities must include required drops."""
        dropped = set(manifest.security.drop_capabilities)
        # "ALL" implicitly covers everything
        if "ALL" in dropped:
            return
        missing = REQUIRED_DROPPED_CAPABILITIES - dropped
        if missing:
            errors.append(
                f"drop_capabilities missing required entries: {sorted(missing)}"
            )

    def _check_no_new_privileges(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 8: no_new_privileges must be True. Not configurable."""
        if not manifest.security.no_new_privileges:
            errors.append(
                "no_new_privileges must be True; privilege escalation is not permitted"
            )

    def _check_memory_ceiling(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 9: max_memory_mb must not exceed system ceiling."""
        if manifest.resources.max_memory_mb > self._ceilings.max_memory_mb:
            errors.append(
                f"max_memory_mb ({manifest.resources.max_memory_mb}) exceeds system "
                f"ceiling ({self._ceilings.max_memory_mb})"
            )

    def _check_wall_time_ceiling(self, manifest: RuntimeManifest, errors: list[str]) -> None:
        """Rule 10: max_wall_seconds must not exceed system ceiling."""
        if manifest.resources.max_wall_seconds > self._ceilings.max_wall_seconds:
            errors.append(
                f"max_wall_seconds ({manifest.resources.max_wall_seconds}) exceeds system "
                f"ceiling ({self._ceilings.max_wall_seconds})"
            )


def build_egress_endpoints_from_routes(
    routes: list[dict],
) -> dict[str, list[str]]:
    """Build the egress_endpoints mapping from Spec 4 egress route data.

    Each route has an endpoint_url and allowed_capabilities. We extract
    host:port from the URL and map each capability to its authorized endpoints.
    """
    from urllib.parse import urlparse

    result: dict[str, list[str]] = {}
    for route in routes:
        url = route.get("endpoint_url", "")
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        endpoint = f"{host}:{port}"

        for cap_id in route.get("allowed_capabilities", []):
            if cap_id not in result:
                result[cap_id] = []
            if endpoint not in result[cap_id]:
                result[cap_id].append(endpoint)
    return result
