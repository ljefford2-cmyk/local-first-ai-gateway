"""Phase 6A unit tests — RuntimeManifest schema, ManifestValidator, audit events.

Target: 25+ tests covering schema, happy-path validation, rejection per rule,
event structure, and integration with Spec 2 capability registry.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from runtime_manifest import (
    ALLOWED_VOLUME_BASE_PATHS,
    AUDIT_LOG_PATH,
    DEFAULT_MAX_MEMORY_MB,
    DEFAULT_MAX_WALL_SECONDS,
    FORBIDDEN_VOLUME_PATHS,
    REQUIRED_DROPPED_CAPABILITIES,
    VALID_WORKER_TYPES,
    WORKER_TYPE_MIN_WAL,
    NetworkPolicy,
    ResourceLimits,
    RuntimeManifest,
    SecurityPolicy,
    SystemResourceCeilings,
    VolumeMount,
)
from manifest_validator import (
    ManifestValidator,
    ManifestValidationResult,
    build_egress_endpoints_from_routes,
)
from events import (
    event_manifest_validated,
    event_manifest_rejected,
)
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager


# UUIDv7 pattern for event validation
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# UUID4 pattern for manifest_id
UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Minimal capabilities config for testing
MINIMAL_CAPS = {
    "route.local": {
        "capability_id": "route.local",
        "capability_name": "Local Ollama Dispatch",
        "capability_type": "governing",
        "desired_wal_level": 0,
        "max_wal": 2,
        "declared_pipeline": [],
        "provider_dependencies": None,
        "action_policies": {
            "0": {"dispatch_local": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "1": {"dispatch_local": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "2": {},
            "3": {},
        },
        "promotion_criteria": {"0_to_1": None, "1_to_2": None, "2_to_3": None},
    },
    "route.cloud.claude": {
        "capability_id": "route.cloud.claude",
        "capability_name": "Claude Cloud Dispatch",
        "capability_type": "governing",
        "desired_wal_level": 0,
        "max_wal": 2,
        "declared_pipeline": [],
        "provider_dependencies": None,
        "action_policies": {
            "0": {"dispatch_cloud": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "1": {"dispatch_cloud": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "2": {},
            "3": {},
        },
        "promotion_criteria": {"0_to_1": None, "1_to_2": None, "2_to_3": None},
    },
}

# Egress routes for testing (mirrors config/egress.json structure)
TEST_EGRESS_ROUTES = [
    {
        "endpoint_url": "https://api.anthropic.com/v1/messages",
        "allowed_capabilities": ["route.cloud.claude"],
    },
    {
        "endpoint_url": "http://ollama:11434/api/generate",
        "allowed_capabilities": ["route.local"],
    },
]


def _make_registry(caps: dict | None = None) -> CapabilityRegistry:
    """Create a CapabilityRegistry loaded from a temp file."""
    data = caps or MINIMAL_CAPS
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(data, f)
        path = f.name
    try:
        reg = CapabilityRegistry(config_path=path)
        reg.load()
        return reg
    finally:
        os.unlink(path)


def _make_state(registry: CapabilityRegistry) -> CapabilityStateManager:
    """Create a CapabilityStateManager initialized from a registry."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        path = f.name
    sm = CapabilityStateManager(state_path=path)
    sm.initialize_from_registry(registry)
    return sm


def _make_egress_endpoints() -> dict[str, list[str]]:
    """Build egress endpoint mapping from test routes."""
    return build_egress_endpoints_from_routes(TEST_EGRESS_ROUTES)


def _make_validator(
    caps: dict | None = None,
    ceilings: SystemResourceCeilings | None = None,
) -> tuple[ManifestValidator, CapabilityRegistry, CapabilityStateManager]:
    """Build a complete validator with registry, state, and egress endpoints."""
    reg = _make_registry(caps)
    sm = _make_state(reg)
    endpoints = _make_egress_endpoints()
    v = ManifestValidator(reg, sm, egress_endpoints=endpoints, ceilings=ceilings)
    return v, reg, sm


def _valid_local_manifest(**overrides) -> RuntimeManifest:
    """Create a valid manifest for an ollama_local worker against route.local."""
    kwargs = dict(
        capability_id="route.local",
        worker_type="ollama_local",
        volumes=[VolumeMount(path="/work", mode="rw", host_path="/data/work")],
        network=NetworkPolicy(
            egress_allow=["ollama:11434"],
            egress_deny_all=True,
            ingress="none",
        ),
        resources=ResourceLimits(
            max_memory_mb=256,
            max_cpu_seconds=60,
            max_wall_seconds=120,
            max_disk_mb=100,
        ),
        security=SecurityPolicy(
            drop_capabilities=["ALL"],
            read_only_root=True,
            no_new_privileges=True,
            seccomp_profile="default",
        ),
    )
    kwargs.update(overrides)
    return RuntimeManifest(**kwargs)


def _valid_cloud_manifest(**overrides) -> RuntimeManifest:
    """Create a valid manifest for a cloud_adapter worker against route.cloud.claude."""
    kwargs = dict(
        capability_id="route.cloud.claude",
        worker_type="cloud_adapter",
        volumes=[VolumeMount(path="/inbox", mode="ro", host_path="/data/inbox")],
        network=NetworkPolicy(
            egress_allow=["api.anthropic.com:443"],
            egress_deny_all=True,
            ingress="none",
        ),
        resources=ResourceLimits(
            max_memory_mb=256,
            max_cpu_seconds=30,
            max_wall_seconds=120,
            max_disk_mb=50,
        ),
        security=SecurityPolicy(
            drop_capabilities=["ALL"],
            read_only_root=True,
            no_new_privileges=True,
            seccomp_profile="default",
        ),
    )
    kwargs.update(overrides)
    return RuntimeManifest(**kwargs)


def _assert_valid_envelope(evt: dict, *, event_type: str, source: str) -> None:
    """Validate an event envelope against the Spec 1 schema."""
    assert evt["schema_version"] == "2.0"
    assert UUID_V7_RE.match(evt["source_event_id"]), f"Bad source_event_id: {evt['source_event_id']}"
    assert isinstance(evt["timestamp"], str) and len(evt["timestamp"]) > 0
    assert evt["event_type"] == event_type
    assert evt["source"] == source
    assert evt["durability"] == "durable"
    assert isinstance(evt["payload"], dict)
    for key in ("job_id", "parent_job_id", "capability_id", "wal_level"):
        assert key in evt, f"Missing nullable field: {key}"


# =============================================================================
# a. Schema Tests (6 tests)
# =============================================================================

class TestRuntimeManifestSchema:
    def test_manifest_id_is_uuid4(self):
        m = _valid_local_manifest()
        assert UUID_V4_RE.match(m.manifest_id), f"Not UUID4: {m.manifest_id}"

    def test_created_at_iso_format(self):
        m = _valid_local_manifest()
        assert "T" in m.created_at
        assert m.created_at.endswith("Z")

    def test_default_network_policy(self):
        np = NetworkPolicy()
        assert np.egress_deny_all is True
        assert np.ingress == "none"
        assert np.egress_allow == []

    def test_default_security_policy(self):
        sp = SecurityPolicy()
        assert sp.drop_capabilities == ["ALL"]
        assert sp.read_only_root is True
        assert sp.no_new_privileges is True
        assert sp.seccomp_profile == "default"

    def test_volume_mount_fields(self):
        vol = VolumeMount(path="/work", mode="rw", host_path="/data/work")
        assert vol.path == "/work"
        assert vol.mode == "rw"
        assert vol.host_path == "/data/work"

    def test_resource_limits_defaults(self):
        rl = ResourceLimits()
        assert rl.max_memory_mb == 256
        assert rl.max_cpu_seconds == 60
        assert rl.max_wall_seconds == 120
        assert rl.max_disk_mb == 100


# =============================================================================
# b. Validation Happy Path (3 tests)
# =============================================================================

class TestValidationHappyPath:
    def test_valid_local_manifest_passes(self):
        v, _, _ = _make_validator()
        result = v.validate(_valid_local_manifest())
        assert result.valid is True
        assert result.errors == []

    def test_valid_cloud_manifest_passes(self):
        v, _, _ = _make_validator()
        result = v.validate(_valid_cloud_manifest())
        assert result.valid is True
        assert result.errors == []

    def test_valid_manifest_with_multiple_volumes(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            volumes=[
                VolumeMount(path="/work", mode="rw", host_path="/data/work"),
                VolumeMount(path="/inbox", mode="ro", host_path="/data/inbox"),
                VolumeMount(path="/outbox", mode="rw", host_path="/data/outbox"),
            ],
        )
        result = v.validate(m)
        assert result.valid is True


# =============================================================================
# c. Validation Rejection Tests (12 tests — at least 1 per rule)
# =============================================================================

class TestValidationRejections:
    def test_unknown_capability_id(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(capability_id="nonexistent.cap")
        result = v.validate(m)
        assert result.valid is False
        assert any("not found in capability registry" in e for e in result.errors)

    def test_suspended_capability(self):
        v, _, sm = _make_validator()
        sm.suspend("route.local")
        m = _valid_local_manifest()
        result = v.validate(m)
        assert result.valid is False
        assert any("suspended" in e for e in result.errors)

    def test_volume_at_etc(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            volumes=[VolumeMount(path="/etc", mode="ro", host_path="/host/etc")]
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("forbidden mount point" in e for e in result.errors)

    def test_volume_at_root(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            volumes=[VolumeMount(path="/", mode="ro", host_path="/")]
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("forbidden mount point" in e for e in result.errors)

    def test_volume_overlapping_audit_log(self):
        v, _, _ = _make_validator()
        # /var/drnt/audit is the audit log path; a parent path mounted rw overlaps
        m = _valid_local_manifest(
            volumes=[VolumeMount(path="/work", mode="rw", host_path="/data/work")]
        )
        # That's fine. But a volume at the audit path itself is bad:
        m2 = RuntimeManifest(
            capability_id="route.local",
            worker_type="ollama_local",
            volumes=[VolumeMount(path="/var/drnt/audit", mode="rw", host_path="/audit")],
            network=NetworkPolicy(egress_allow=["ollama:11434"], egress_deny_all=True),
            security=SecurityPolicy(drop_capabilities=["ALL"], no_new_privileges=True),
        )
        result = v.validate(m2)
        assert result.valid is False
        assert any("audit log path" in e for e in result.errors)

    def test_egress_deny_all_false(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            network=NetworkPolicy(
                egress_allow=["ollama:11434"],
                egress_deny_all=False,
                ingress="none",
            ),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("egress_deny_all must be True" in e for e in result.errors)

    def test_endpoint_not_in_egress_policy(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            network=NetworkPolicy(
                egress_allow=["evil.example.com:443"],
                egress_deny_all=True,
                ingress="none",
            ),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("not authorized" in e for e in result.errors)

    def test_missing_required_dropped_capabilities(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            security=SecurityPolicy(
                drop_capabilities=["NET_RAW"],  # Missing SYS_ADMIN, SYS_PTRACE, MKNOD
                no_new_privileges=True,
            ),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("drop_capabilities missing" in e for e in result.errors)

    def test_no_new_privileges_false(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            security=SecurityPolicy(
                drop_capabilities=["ALL"],
                no_new_privileges=False,
            ),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("no_new_privileges must be True" in e for e in result.errors)

    def test_memory_over_ceiling(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            resources=ResourceLimits(max_memory_mb=1024),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("max_memory_mb" in e and "exceeds" in e for e in result.errors)

    def test_wall_time_over_ceiling(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            resources=ResourceLimits(max_wall_seconds=600),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("max_wall_seconds" in e and "exceeds" in e for e in result.errors)

    def test_wal_too_low_for_tool_executor(self):
        """tool_executor requires WAL >= 1; route.local starts at WAL 0."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest(worker_type="tool_executor")
        result = v.validate(m)
        assert result.valid is False
        assert any("requires WAL >= 1" in e for e in result.errors)


# =============================================================================
# d. Warning Tests (1 test)
# =============================================================================

class TestValidationWarnings:
    def test_tmp_rw_warning(self):
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            volumes=[
                VolumeMount(path="/work", mode="rw", host_path="/data/work"),
                VolumeMount(path="/tmp", mode="rw", host_path="/data/tmp"),
            ],
        )
        result = v.validate(m)
        assert result.valid is True
        assert any("/tmp rw" in w for w in result.warnings)


# =============================================================================
# e. Custom Ceiling Tests (2 tests)
# =============================================================================

class TestCustomCeilings:
    def test_custom_memory_ceiling(self):
        ceilings = SystemResourceCeilings(max_memory_mb=128)
        v, _, _ = _make_validator(ceilings=ceilings)
        m = _valid_local_manifest(
            resources=ResourceLimits(max_memory_mb=200),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("128" in e for e in result.errors)

    def test_custom_wall_ceiling(self):
        ceilings = SystemResourceCeilings(max_wall_seconds=60)
        v, _, _ = _make_validator(ceilings=ceilings)
        m = _valid_local_manifest(
            resources=ResourceLimits(max_wall_seconds=120),
        )
        result = v.validate(m)
        assert result.valid is False
        assert any("60" in e for e in result.errors)


# =============================================================================
# f. Event Tests (4 tests)
# =============================================================================

class TestManifestEvents:
    def test_manifest_validated_event_envelope(self):
        evt = event_manifest_validated(
            manifest_id="test-id",
            capability_id="route.local",
            worker_type="ollama_local",
            valid=True,
            error_count=0,
            warning_count=0,
        )
        _assert_valid_envelope(evt, event_type="manifest.validated", source="orchestrator")

    def test_manifest_validated_payload_fields(self):
        evt = event_manifest_validated(
            manifest_id="m-123",
            capability_id="route.cloud.claude",
            worker_type="cloud_adapter",
            valid=True,
            error_count=0,
            warning_count=1,
        )
        p = evt["payload"]
        assert p["manifest_id"] == "m-123"
        assert p["capability_id"] == "route.cloud.claude"
        assert p["worker_type"] == "cloud_adapter"
        assert p["valid"] is True
        assert p["error_count"] == 0
        assert p["warning_count"] == 1
        assert "errors" not in p  # No errors when valid

    def test_manifest_validated_includes_errors_when_invalid(self):
        evt = event_manifest_validated(
            manifest_id="m-456",
            capability_id="bad.cap",
            worker_type="ollama_local",
            valid=False,
            error_count=2,
            warning_count=0,
            errors=["error one", "error two"],
        )
        p = evt["payload"]
        assert p["valid"] is False
        assert p["errors"] == ["error one", "error two"]

    def test_manifest_rejected_event_envelope(self):
        evt = event_manifest_rejected(
            manifest_id="m-789",
            capability_id="route.local",
            rejection_reason="capability not found",
            all_errors=["capability not found", "egress denied"],
        )
        _assert_valid_envelope(evt, event_type="manifest.rejected", source="orchestrator")
        p = evt["payload"]
        assert p["manifest_id"] == "m-789"
        assert p["rejection_reason"] == "capability not found"
        assert p["all_errors"] == ["capability not found", "egress denied"]


# =============================================================================
# g. Integration with Spec 2 (3 tests)
# =============================================================================

class TestSpec2Integration:
    def test_capability_state_change_invalidates_manifest(self):
        """Suspending a capability causes a previously valid manifest to fail."""
        v, _, sm = _make_validator()
        m = _valid_local_manifest()
        # Valid initially
        result = v.validate(m)
        assert result.valid is True
        # Suspend capability
        sm.suspend("route.local")
        result = v.validate(m)
        assert result.valid is False
        assert any("suspended" in e for e in result.errors)

    def test_wal_demotion_invalidates_tool_executor(self):
        """A tool_executor needs WAL >= 1. If WAL drops to 0, manifest fails."""
        v, _, sm = _make_validator()
        sm.set_effective_wal("route.local", 1)
        m = _valid_local_manifest(worker_type="tool_executor")
        # Valid at WAL 1
        result = v.validate(m)
        assert result.valid is True
        # Demote to WAL 0
        sm.demote("route.local", 0)
        result = v.validate(m)
        assert result.valid is False
        assert any("requires WAL >= 1" in e for e in result.errors)

    def test_multiple_errors_reported_simultaneously(self):
        """Validator reports ALL violations, not just the first one."""
        v, _, sm = _make_validator()
        sm.suspend("route.local")
        m = RuntimeManifest(
            capability_id="route.local",
            worker_type="ollama_local",
            volumes=[VolumeMount(path="/etc", mode="rw", host_path="/host/etc")],
            network=NetworkPolicy(
                egress_allow=["evil.com:443"],
                egress_deny_all=False,
            ),
            security=SecurityPolicy(
                drop_capabilities=["NET_RAW"],
                no_new_privileges=False,
            ),
            resources=ResourceLimits(
                max_memory_mb=2048,
                max_wall_seconds=9999,
            ),
        )
        result = v.validate(m)
        assert result.valid is False
        # Should have errors for: suspended, forbidden path, egress_deny_all,
        # unauthorized endpoint, missing drops, no_new_privileges, memory, wall_time
        assert len(result.errors) >= 7


# =============================================================================
# h. build_egress_endpoints_from_routes utility (2 tests)
# =============================================================================

class TestBuildEgressEndpoints:
    def test_parses_https_endpoint(self):
        routes = [
            {
                "endpoint_url": "https://api.anthropic.com/v1/messages",
                "allowed_capabilities": ["route.cloud.claude"],
            },
        ]
        result = build_egress_endpoints_from_routes(routes)
        assert "route.cloud.claude" in result
        assert "api.anthropic.com:443" in result["route.cloud.claude"]

    def test_parses_http_endpoint(self):
        routes = [
            {
                "endpoint_url": "http://ollama:11434/api/generate",
                "allowed_capabilities": ["route.local"],
            },
        ]
        result = build_egress_endpoints_from_routes(routes)
        assert "route.local" in result
        assert "ollama:11434" in result["route.local"]


# =============================================================================
# i. Constants / Module-level assertions (2 tests)
# =============================================================================

class TestManifestConstants:
    def test_valid_worker_types(self):
        assert VALID_WORKER_TYPES == {"ollama_local", "cloud_adapter", "tool_executor"}

    def test_required_dropped_capabilities(self):
        assert REQUIRED_DROPPED_CAPABILITIES == {"NET_RAW", "SYS_ADMIN", "SYS_PTRACE", "MKNOD"}
