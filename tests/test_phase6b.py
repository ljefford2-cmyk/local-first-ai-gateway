"""Phase 6B unit tests — SandboxBlueprint, BlueprintEngine, audit events.

Target: 15+ tests covering blueprint generation, security hardening,
rejection rules, event structure, and integration with Phase 6A.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from runtime_manifest import (
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
from sandbox_blueprint import (
    ContainerConfig,
    ContainerMount,
    ContainerNetworkConfig,
    ContainerResourceConfig,
    ContainerSecurityConfig,
    SandboxBlueprint,
)
from blueprint_engine import BlueprintEngine, DEFAULT_BASE_DIR, DEFAULT_IMAGE
from events import event_blueprint_created
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager


# UUIDv7 pattern for event validation
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# UUID4 pattern for blueprint/manifest IDs
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


def _make_registry(caps=None):
    data = caps or MINIMAL_CAPS
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    try:
        reg = CapabilityRegistry(config_path=path)
        reg.load()
        return reg
    finally:
        os.unlink(path)


def _make_state(registry):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    sm = CapabilityStateManager(state_path=path)
    sm.initialize_from_registry(registry)
    return sm


def _make_validator(caps=None, ceilings=None):
    reg = _make_registry(caps)
    sm = _make_state(reg)
    endpoints = build_egress_endpoints_from_routes(TEST_EGRESS_ROUTES)
    v = ManifestValidator(reg, sm, egress_endpoints=endpoints, ceilings=ceilings)
    return v, reg, sm


def _valid_local_manifest(**overrides):
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


def _valid_cloud_manifest(**overrides):
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


def _validate_and_generate(manifest, validator, engine=None):
    """Validate a manifest and generate a blueprint. Returns (blueprint, result)."""
    engine = engine or BlueprintEngine()
    result = validator.validate(manifest)
    bp = engine.generate(manifest, result)
    return bp, result


def _assert_valid_envelope(evt, *, event_type, source):
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
# a. Blueprint Generation Tests (5+ tests)
# =============================================================================

class TestBlueprintGeneration:
    def test_valid_local_manifest_produces_blueprint(self):
        """A valid local manifest produces a complete SandboxBlueprint."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert isinstance(bp, SandboxBlueprint)
        assert UUID_V4_RE.match(bp.blueprint_id)
        assert bp.manifest_id == m.manifest_id
        assert bp.capability_id == m.capability_id
        assert "T" in bp.created_at and bp.created_at.endswith("Z")

    def test_volume_translations_correct(self):
        """VolumeMount list translates to ContainerMount list with correct fields."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            volumes=[
                VolumeMount(path="/work", mode="rw", host_path="/data/work"),
                VolumeMount(path="/inbox", mode="ro", host_path="/data/inbox"),
            ]
        )
        bp, _ = _validate_and_generate(m, v)
        assert len(bp.mounts) == 2
        # /work rw -> read_only=False
        work_mount = [mt for mt in bp.mounts if mt.target == "/work"][0]
        assert work_mount.read_only is False
        assert work_mount.source.endswith("/data/work")
        # /inbox ro -> read_only=True
        inbox_mount = [mt for mt in bp.mounts if mt.target == "/inbox"][0]
        assert inbox_mount.read_only is True

    def test_network_mode_egress_proxy(self):
        """Non-empty egress_allow sets network_mode to drnt-internal."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()  # has egress_allow=["ollama:11434"]
        bp, _ = _validate_and_generate(m, v)
        assert bp.network_config.network_mode == "drnt-internal"

    def test_network_mode_none_when_no_egress(self):
        """Empty egress_allow sets network_mode to none."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            network=NetworkPolicy(
                egress_allow=[],
                egress_deny_all=True,
                ingress="none",
            ),
        )
        bp, _ = _validate_and_generate(m, v)
        assert bp.network_config.network_mode == "none"

    def test_resource_limits_translated(self):
        """Manifest resource limits map to container resource config."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            resources=ResourceLimits(
                max_memory_mb=128,
                max_cpu_seconds=30,
                max_wall_seconds=60,
                max_disk_mb=200,
            ),
        )
        bp, _ = _validate_and_generate(m, v)
        assert bp.resource_config.memory_limit == "128m"
        assert bp.resource_config.cpu_period == 100000
        assert bp.resource_config.cpu_quota == 30 * 100000
        assert bp.resource_config.storage_opt == {"size": "200m"}

    def test_labels_injected(self):
        """Standard drnt labels are set on the container config."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        labels = bp.container_config.labels
        assert labels["drnt.manifest_id"] == m.manifest_id
        assert labels["drnt.capability_id"] == m.capability_id
        assert labels["drnt.worker_type"] == m.worker_type
        assert "drnt.created_at" in labels

    def test_custom_image_and_base_dir(self):
        """Engine respects custom image and base_dir configuration."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        engine = BlueprintEngine(base_dir="/custom/base", image="custom:v2")
        result = v.validate(m)
        bp = engine.generate(m, result)
        assert bp.container_config.image == "custom:v2"
        assert bp.mounts[0].source.startswith("/custom/base")


# =============================================================================
# b. Security Hardening Tests (5+ tests)
# =============================================================================

class TestSecurityHardening:
    def test_all_caps_dropped(self):
        """ALL Linux capabilities are dropped."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert "ALL" in bp.security_config.cap_drop

    def test_read_only_rootfs_enforced_even_if_manifest_says_false(self):
        """read_only_rootfs is always True, regardless of manifest declaration."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest(
            security=SecurityPolicy(
                drop_capabilities=["ALL"],
                read_only_root=False,  # Manifest says False
                no_new_privileges=True,
                seccomp_profile="default",
            ),
        )
        bp, _ = _validate_and_generate(m, v)
        assert bp.security_config.read_only_rootfs is True  # Blueprint overrides

    def test_tmpfs_has_noexec(self):
        """tmpfs mount includes noexec and nosuid flags."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert len(bp.security_config.tmpfs_mounts) == 1
        tmpfs = bp.security_config.tmpfs_mounts[0]
        assert "noexec" in tmpfs
        assert "nosuid" in tmpfs
        assert "/tmp" in tmpfs
        assert "64m" in tmpfs

    def test_no_secrets_in_environment(self):
        """Environment dict is empty — no secrets injected."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert bp.container_config.environment == {}

    def test_pids_limit_set(self):
        """Pids limit is set to 256."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert bp.resource_config.pids_limit == 256

    def test_cloud_adapter_gets_net_bind_service(self):
        """cloud_adapter workers get NET_BIND_SERVICE added back."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert "NET_BIND_SERVICE" in bp.security_config.cap_add
        assert "ALL" in bp.security_config.cap_drop  # Still drops ALL first

    def test_local_worker_no_cap_add(self):
        """ollama_local workers get no capabilities added back."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert bp.security_config.cap_add == []

    def test_dns_empty(self):
        """Workers do not resolve DNS directly — dns list is empty."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert bp.network_config.dns == []

    def test_no_new_privileges_enforced(self):
        """no_new_privileges is always True in the blueprint."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        bp, _ = _validate_and_generate(m, v)
        assert bp.security_config.no_new_privileges is True


# =============================================================================
# c. Rejection Tests (3+ tests)
# =============================================================================

class TestBlueprintRejection:
    def test_invalid_manifest_rejected(self):
        """Blueprint generation fails when validation_result.valid is False."""
        engine = BlueprintEngine()
        m = _valid_local_manifest()
        bad_result = ManifestValidationResult(
            valid=False,
            errors=["some error"],
        )
        try:
            engine.generate(m, bad_result)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "invalid manifest" in str(e).lower()

    def test_missing_validation_result_rejected(self):
        """Blueprint generation fails when validation_result is not the right type."""
        engine = BlueprintEngine()
        m = _valid_local_manifest()
        try:
            engine.generate(m, None)  # type: ignore
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "ManifestValidationResult" in str(e)

    def test_non_result_object_rejected(self):
        """Blueprint generation fails when given a plain dict instead of result."""
        engine = BlueprintEngine()
        m = _valid_local_manifest()
        try:
            engine.generate(m, {"valid": True})  # type: ignore
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "ManifestValidationResult" in str(e)

    def test_result_with_errors_rejected(self):
        """A result with valid=False and multiple errors is rejected."""
        engine = BlueprintEngine()
        m = _valid_local_manifest()
        result = ManifestValidationResult(
            valid=False,
            errors=["error 1", "error 2", "error 3"],
        )
        try:
            engine.generate(m, result)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "error 1" in str(e)


# =============================================================================
# d. Event Tests (2 tests)
# =============================================================================

class TestBlueprintEvents:
    def test_blueprint_created_event_envelope(self):
        """sandbox.blueprint_created event has correct envelope structure."""
        evt = event_blueprint_created(
            blueprint_id="bp-123",
            manifest_id="m-456",
            capability_id="route.local",
            network_mode="none",
            mount_count=2,
            cap_drop_count=1,
            memory_limit="256m",
        )
        _assert_valid_envelope(evt, event_type="sandbox.blueprint_created", source="orchestrator")

    def test_blueprint_created_payload_fields(self):
        """sandbox.blueprint_created payload contains all required fields."""
        evt = event_blueprint_created(
            blueprint_id="bp-789",
            manifest_id="m-012",
            capability_id="route.cloud.claude",
            network_mode="drnt-egress-proxy",
            mount_count=1,
            cap_drop_count=1,
            memory_limit="512m",
        )
        p = evt["payload"]
        assert p["blueprint_id"] == "bp-789"
        assert p["manifest_id"] == "m-012"
        assert p["capability_id"] == "route.cloud.claude"
        assert p["network_mode"] == "drnt-egress-proxy"
        assert p["mount_count"] == 1
        assert p["cap_drop_count"] == 1
        assert p["memory_limit"] == "512m"


# =============================================================================
# e. Integration with 6A (2+ tests)
# =============================================================================

class TestIntegrationWith6A:
    def test_end_to_end_manifest_validate_blueprint(self):
        """End-to-end: create manifest -> validate -> generate blueprint."""
        v, _, _ = _make_validator()
        m = _valid_local_manifest()
        result = v.validate(m)
        assert result.valid is True
        engine = BlueprintEngine()
        bp = engine.generate(m, result)
        assert isinstance(bp, SandboxBlueprint)
        assert bp.manifest_id == m.manifest_id
        assert bp.capability_id == "route.local"
        assert bp.network_config.network_mode == "drnt-internal"
        assert bp.security_config.read_only_rootfs is True
        assert "ALL" in bp.security_config.cap_drop

    def test_capability_demotion_blocks_blueprint(self):
        """Capability demotion invalidates manifest, blocking blueprint generation."""
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
        engine = BlueprintEngine()
        try:
            engine.generate(m, result)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected

    def test_suspended_capability_blocks_blueprint(self):
        """Suspending a capability prevents blueprint generation."""
        v, _, sm = _make_validator()
        m = _valid_local_manifest()
        # Valid initially
        result = v.validate(m)
        assert result.valid is True
        # Suspend
        sm.suspend("route.local")
        result = v.validate(m)
        assert result.valid is False
        engine = BlueprintEngine()
        try:
            engine.generate(m, result)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass  # Expected
