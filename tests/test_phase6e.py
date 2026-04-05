"""Phase 6E unit tests — Worker Lifecycle Integration.

Target: 18+ tests covering lifecycle happy path, failure handling,
pipeline integration, override integration, and full signal chain.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import pytest

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
from sandbox_blueprint import SandboxBlueprint
from blueprint_engine import BlueprintEngine
from egress_proxy import EgressProxy, EgressDecision, EgressPolicy
from egress_rate_limiter import EgressRateLimiter
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from worker_context import WorkerContext
from worker_lifecycle import (
    EgressPolicyStore,
    WorkerLifecycle,
    WorkerPreparationError,
)
from events import (
    event_worker_prepared,
    event_worker_teardown,
    event_worker_preparation_failed,
)
from models import Job, JobStatus
from test_helpers import MockAuditClient

# UUIDv7 pattern for event validation
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# UUID4 pattern for worker_id
UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_capabilities_file(tmp_dir: str) -> str:
    """Write a minimal capabilities.json and return its path."""
    caps = {
        "route.local": {
            "capability_id": "route.local",
            "capability_name": "Local Routing",
            "capability_type": "governing",
            "desired_wal_level": 0,
            "max_wal": 3,
            "declared_pipeline": [],
            "provider_dependencies": None,
            "action_policies": {
                "0": {"dispatch_local": {"review_gate": "none"}},
                "1": {"dispatch_local": {"review_gate": "none"}},
                "2": {"dispatch_local": {"review_gate": "none"}},
                "3": {"dispatch_local": {"review_gate": "none"}},
            },
            "promotion_criteria": {
                "0_to_1": None,
                "1_to_2": None,
                "2_to_3": None,
            },
        },
        "route.cloud.general": {
            "capability_id": "route.cloud.general",
            "capability_name": "Cloud General",
            "capability_type": "governing",
            "desired_wal_level": 0,
            "max_wal": 3,
            "declared_pipeline": [],
            "provider_dependencies": None,
            "action_policies": {
                "0": {"dispatch_cloud": {"review_gate": "none"}},
                "1": {"dispatch_cloud": {"review_gate": "none"}},
                "2": {"dispatch_cloud": {"review_gate": "none"}},
                "3": {"dispatch_cloud": {"review_gate": "none"}},
            },
            "promotion_criteria": {
                "0_to_1": None,
                "1_to_2": None,
                "2_to_3": None,
            },
        },
    }
    path = os.path.join(tmp_dir, "capabilities.json")
    with open(path, "w") as f:
        json.dump(caps, f)
    return path


def _make_state_manager(registry: CapabilityRegistry, tmp_dir: str) -> CapabilityStateManager:
    """Create a state manager initialized from the registry."""
    state_path = os.path.join(tmp_dir, "capabilities.state.json")
    mgr = CapabilityStateManager(state_path=state_path)
    mgr.initialize_from_registry(registry)
    return mgr


def _make_egress_policy_store() -> EgressPolicyStore:
    """Create a policy store with a route.cloud.general binding."""
    store = EgressPolicyStore()
    store.set("route.cloud.general", EgressPolicy(
        capability_id="route.cloud.general",
        allowed_endpoints=["api.anthropic.com:443"],
        rate_limit_rpm=30,
    ))
    return store


def _make_lifecycle(tmp_dir: str, **overrides):
    """Build a fully wired WorkerLifecycle with sane defaults."""
    caps_path = _make_capabilities_file(tmp_dir)
    registry = CapabilityRegistry(config_path=caps_path)
    registry.load()

    state_mgr = overrides.get("state_manager") or _make_state_manager(registry, tmp_dir)
    egress_endpoints = build_egress_endpoints_from_routes([
        {
            "endpoint_url": "https://api.anthropic.com/v1/messages",
            "allowed_capabilities": ["route.cloud.general"],
        },
    ])
    validator = ManifestValidator(
        registry=registry,
        state_manager=state_mgr,
        egress_endpoints=egress_endpoints,
    )
    engine = BlueprintEngine()
    policy_store = _make_egress_policy_store()
    audit = MockAuditClient()

    lifecycle = WorkerLifecycle(
        manifest_validator=validator,
        blueprint_engine=engine,
        capability_registry=registry,
        egress_policy_store=policy_store,
        audit_client=audit,
        state_manager=state_mgr,
    )
    return lifecycle, audit, registry, state_mgr


def _make_job(capability_id: str = "route.local", **overrides) -> Job:
    """Create a test Job with the given governing capability."""
    return Job(
        job_id=overrides.get("job_id", "test-job-001"),
        raw_input="What is the weather?",
        input_modality="text",
        device="phone",
        status=overrides.get("status", JobStatus.classified.value),
        governing_capability_id=capability_id,
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle happy path (6 tests)
# ---------------------------------------------------------------------------

class TestLifecycleHappyPath:
    """Job -> prepare_worker -> WorkerContext created. Teardown cleans up."""

    @pytest.mark.asyncio
    async def test_prepare_worker_returns_worker_context(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        ctx = await lifecycle.prepare_worker(job)

        assert isinstance(ctx, WorkerContext)
        assert ctx.job_id == "test-job-001"
        assert ctx.status == "prepared"
        assert ctx.manifest.capability_id == "route.local"
        assert ctx.validation_result.valid is True
        assert ctx.blueprint is not None
        assert UUID_V4_RE.match(ctx.worker_id)

    @pytest.mark.asyncio
    async def test_prepare_worker_emits_worker_prepared_event(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        ctx = await lifecycle.prepare_worker(job)

        prepared_events = audit.get_events_by_type("worker.prepared")
        assert len(prepared_events) == 1
        evt = prepared_events[0]
        assert evt["payload"]["worker_id"] == ctx.worker_id
        assert evt["payload"]["job_id"] == "test-job-001"
        assert evt["payload"]["capability_id"] == "route.local"
        assert evt["payload"]["blueprint_id"] == ctx.blueprint.blueprint_id
        assert evt["payload"]["manifest_id"] == ctx.manifest.manifest_id

    @pytest.mark.asyncio
    async def test_teardown_worker_emits_teardown_event(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        await lifecycle.teardown_worker(ctx)

        teardown_events = audit.get_events_by_type("worker.teardown")
        assert len(teardown_events) == 1
        evt = teardown_events[0]
        assert evt["payload"]["worker_id"] == ctx.worker_id
        assert evt["payload"]["job_id"] == "test-job-001"
        assert evt["payload"]["total_egress_requests"] == 0
        assert evt["payload"]["denied_egress_requests"] == 0
        assert evt["payload"]["duration_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_teardown_marks_context_completed(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        ctx = await lifecycle.prepare_worker(job)

        assert ctx.worker_id in lifecycle.active_contexts
        await lifecycle.teardown_worker(ctx)

        assert ctx.status == "completed"
        assert ctx.worker_id not in lifecycle.active_contexts

    @pytest.mark.asyncio
    async def test_prepare_cloud_worker_has_egress_proxy(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.cloud.general")
        ctx = await lifecycle.prepare_worker(job)

        assert ctx.egress_proxy is not None
        # Should allow api.anthropic.com:443
        decision = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_prepare_local_worker_has_no_egress_endpoints(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        ctx = await lifecycle.prepare_worker(job)

        # Local worker should have network_mode "none" -> denies all egress
        decision = ctx.egress_proxy.authorize("evil.com", 443, "GET")
        assert decision.allowed is False


# ---------------------------------------------------------------------------
# 2. Failure handling (5 tests)
# ---------------------------------------------------------------------------

class TestFailureHandling:
    """Manifest validation fails -> job fails with correct error_class."""

    @pytest.mark.asyncio
    async def test_unknown_capability_raises_preparation_error(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("nonexistent.capability")

        with pytest.raises(WorkerPreparationError, match="not found in registry"):
            await lifecycle.prepare_worker(job)

        # Should emit worker_preparation_failed event
        failed = audit.get_events_by_type("job.failed")
        assert len(failed) == 1
        assert failed[0]["payload"]["error_class"] == "worker_preparation_failed"

    @pytest.mark.asyncio
    async def test_suspended_capability_raises_preparation_error(self, tmp_path):
        lifecycle, audit, _, state_mgr = _make_lifecycle(str(tmp_path))

        # Suspend the capability
        state_mgr.suspend("route.local")

        job = _make_job("route.local")
        with pytest.raises(WorkerPreparationError, match="suspended"):
            await lifecycle.prepare_worker(job)

        failed = audit.get_events_by_type("job.failed")
        assert len(failed) == 1
        assert failed[0]["payload"]["error_class"] == "worker_preparation_failed"

    @pytest.mark.asyncio
    async def test_no_governing_capability_raises_preparation_error(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        job.governing_capability_id = None

        with pytest.raises(WorkerPreparationError, match="no governing_capability_id"):
            await lifecycle.prepare_worker(job)

    @pytest.mark.asyncio
    async def test_failure_produces_audit_event_with_capability_id(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("nonexistent.capability")

        with pytest.raises(WorkerPreparationError):
            await lifecycle.prepare_worker(job)

        failed = audit.get_events_by_type("job.failed")
        assert len(failed) == 1
        assert failed[0]["payload"]["failing_capability_id"] == "nonexistent.capability"

    @pytest.mark.asyncio
    async def test_manifest_validation_failure_reports_errors(self, tmp_path):
        """Trigger manifest validation failure via resource ceiling breach."""
        caps_path = _make_capabilities_file(str(tmp_path))
        registry = CapabilityRegistry(config_path=caps_path)
        registry.load()
        state_mgr = _make_state_manager(registry, str(tmp_path))

        # Set very low ceiling so default manifest resources exceed it
        tiny_ceilings = SystemResourceCeilings(max_memory_mb=1, max_wall_seconds=1)
        validator = ManifestValidator(
            registry=registry,
            state_manager=state_mgr,
            ceilings=tiny_ceilings,
        )
        engine = BlueprintEngine()
        policy_store = _make_egress_policy_store()
        audit = MockAuditClient()

        lifecycle = WorkerLifecycle(
            manifest_validator=validator,
            blueprint_engine=engine,
            capability_registry=registry,
            egress_policy_store=policy_store,
            audit_client=audit,
            state_manager=state_mgr,
        )

        job = _make_job("route.local")
        with pytest.raises(WorkerPreparationError, match="manifest validation failed"):
            await lifecycle.prepare_worker(job)


# ---------------------------------------------------------------------------
# 3. Pipeline integration (5 tests)
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """Worker preparation happens after permission check. Teardown on completion."""

    def test_worker_context_dataclass_fields(self):
        """WorkerContext has all required fields."""
        ctx = WorkerContext()
        assert hasattr(ctx, "worker_id")
        assert hasattr(ctx, "job_id")
        assert hasattr(ctx, "manifest")
        assert hasattr(ctx, "validation_result")
        assert hasattr(ctx, "blueprint")
        assert hasattr(ctx, "egress_proxy")
        assert hasattr(ctx, "created_at")
        assert hasattr(ctx, "status")
        assert ctx.status == "prepared"

    @pytest.mark.asyncio
    async def test_egress_proxy_authorization_called_by_cloud_worker(self, tmp_path):
        """EgressProxy.authorize is called and logs events."""
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.cloud.general")
        ctx = await lifecycle.prepare_worker(job)
        audit.clear()

        # Simulate provider adapter calling egress proxy
        decision = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
        ctx.egress_proxy.log_request("api.anthropic.com", 443, decision)

        assert decision.allowed is True
        assert len(ctx.egress_proxy.events) == 1
        assert ctx.egress_proxy.events[0]["event_type"] == "egress.authorized"

        # Teardown should report egress stats
        await lifecycle.teardown_worker(ctx)
        teardown = audit.get_events_by_type("worker.teardown")[0]
        assert teardown["payload"]["total_egress_requests"] == 1
        assert teardown["payload"]["denied_egress_requests"] == 0

    @pytest.mark.asyncio
    async def test_egress_proxy_denies_unauthorized_endpoint(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.cloud.general")
        ctx = await lifecycle.prepare_worker(job)

        decision = ctx.egress_proxy.authorize("evil.com", 443, "GET")
        ctx.egress_proxy.log_request("evil.com", 443, decision)

        assert decision.allowed is False

        # Teardown counts denied
        await lifecycle.teardown_worker(ctx)
        teardown = audit.get_events_by_type("worker.teardown")[0]
        assert teardown["payload"]["denied_egress_requests"] == 1

    @pytest.mark.asyncio
    async def test_rate_limiter_cleared_on_teardown(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        rate_limiter = lifecycle._rate_limiter

        job = _make_job("route.cloud.general")
        ctx = await lifecycle.prepare_worker(job)

        # Simulate some rate limiter usage
        blueprint_id = ctx.blueprint.blueprint_id
        rate_limiter.check(blueprint_id, 30)
        rate_limiter.check(blueprint_id, 30)
        assert rate_limiter.current_count(blueprint_id) == 2

        # After teardown, rate limiter should be cleared
        await lifecycle.teardown_worker(ctx)
        assert rate_limiter.current_count(blueprint_id) == 0

    @pytest.mark.asyncio
    async def test_context_packager_runs_inside_worker_context(self, tmp_path):
        """Verify the blueprint is created before context packaging would run."""
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.cloud.general")
        ctx = await lifecycle.prepare_worker(job)

        # Blueprint exists -- context packager can now reference it
        assert ctx.blueprint is not None
        assert ctx.blueprint.capability_id == "route.cloud.general"
        # The blueprint has network_mode for egress proxy routing
        assert ctx.blueprint.network_config.network_mode == "drnt-internal"


# ---------------------------------------------------------------------------
# 4. Override integration (3 tests)
# ---------------------------------------------------------------------------

class TestOverrideIntegration:
    """Cancel/redirect/escalate triggers teardown of old + prepare of new."""

    @pytest.mark.asyncio
    async def test_cancel_triggers_teardown(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")

        # Prepare worker
        ctx = await lifecycle.prepare_worker(job)
        assert ctx.worker_id in lifecycle.active_contexts

        # Simulate cancel by calling teardown directly (as JobManager would)
        ctx.status = "failed"
        await lifecycle.teardown_worker(ctx)

        assert ctx.status == "completed"
        assert ctx.worker_id not in lifecycle.active_contexts

        teardown = audit.get_events_by_type("worker.teardown")
        assert len(teardown) == 1

    @pytest.mark.asyncio
    async def test_redirect_teardown_old_prepare_new(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))

        # Prepare original worker for local route
        original_job = _make_job("route.local", job_id="job-original")
        original_ctx = await lifecycle.prepare_worker(original_job)

        # Teardown original (redirect)
        original_ctx.status = "failed"
        await lifecycle.teardown_worker(original_ctx)

        # Prepare successor worker for cloud route
        successor_job = _make_job("route.cloud.general", job_id="job-successor")
        successor_ctx = await lifecycle.prepare_worker(successor_job)

        assert original_ctx.status == "completed"
        assert successor_ctx.status == "prepared"
        assert successor_ctx.job_id == "job-successor"

        # Verify events: prepared (original) + teardown + prepared (successor)
        prepared = audit.get_events_by_type("worker.prepared")
        teardown = audit.get_events_by_type("worker.teardown")
        assert len(prepared) == 2
        assert len(teardown) == 1

    @pytest.mark.asyncio
    async def test_escalate_teardown_old_prepare_new_capability(self, tmp_path):
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))

        # Prepare original worker
        original_job = _make_job("route.local", job_id="job-escalate-old")
        original_ctx = await lifecycle.prepare_worker(original_job)

        # Escalate: teardown original
        original_ctx.status = "failed"
        await lifecycle.teardown_worker(original_ctx)

        # Prepare new with different capability
        escalated_job = _make_job("route.cloud.general", job_id="job-escalate-new")
        escalated_ctx = await lifecycle.prepare_worker(escalated_job)

        # New worker has different capability
        assert escalated_ctx.manifest.capability_id == "route.cloud.general"
        assert original_ctx.manifest.capability_id == "route.local"
        assert escalated_ctx.worker_id != original_ctx.worker_id


# ---------------------------------------------------------------------------
# 5. Full signal chain (5 tests)
# ---------------------------------------------------------------------------

class TestFullSignalChain:
    """End-to-end lifecycle from prepare through teardown."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_event_sequence(self, tmp_path):
        """prepare -> active -> egress -> teardown produces correct event order."""
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.cloud.general", job_id="e2e-job-001")

        # Prepare
        ctx = await lifecycle.prepare_worker(job)
        ctx.status = "active"

        # Simulate egress activity
        d1 = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
        ctx.egress_proxy.log_request("api.anthropic.com", 443, d1)
        d2 = ctx.egress_proxy.authorize("evil.com", 443, "GET")
        ctx.egress_proxy.log_request("evil.com", 443, d2)

        # Teardown
        await lifecycle.teardown_worker(ctx)

        # Verify full event chain
        types = [e["event_type"] for e in audit.events]
        assert "worker.prepared" in types
        assert "worker.teardown" in types
        # worker.prepared must come before worker.teardown
        prepared_idx = types.index("worker.prepared")
        teardown_idx = types.index("worker.teardown")
        assert prepared_idx < teardown_idx

        # Teardown payload has correct egress stats
        teardown_evt = audit.get_events_by_type("worker.teardown")[0]
        assert teardown_evt["payload"]["total_egress_requests"] == 2
        assert teardown_evt["payload"]["denied_egress_requests"] == 1
        assert teardown_evt["payload"]["duration_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_multiple_workers_independent(self, tmp_path):
        """Two workers for different jobs don't interfere."""
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))

        job_a = _make_job("route.local", job_id="job-A")
        job_b = _make_job("route.cloud.general", job_id="job-B")

        ctx_a = await lifecycle.prepare_worker(job_a)
        ctx_b = await lifecycle.prepare_worker(job_b)

        assert ctx_a.worker_id != ctx_b.worker_id
        assert len(lifecycle.active_contexts) == 2

        # Teardown A, B stays active
        await lifecycle.teardown_worker(ctx_a)
        assert len(lifecycle.active_contexts) == 1
        assert ctx_b.worker_id in lifecycle.active_contexts

        # Teardown B
        await lifecycle.teardown_worker(ctx_b)
        assert len(lifecycle.active_contexts) == 0

    def test_event_builders_produce_valid_envelopes(self):
        """All worker lifecycle events have correct schema."""
        # worker.prepared
        evt = event_worker_prepared(
            worker_id="w-001", job_id="j-001",
            capability_id="route.local",
            blueprint_id="bp-001", manifest_id="m-001",
        )
        assert evt["event_type"] == "worker.prepared"
        assert evt["source"] == "orchestrator"
        assert evt["schema_version"] == "2.0"
        assert UUID_V7_RE.match(evt["source_event_id"])
        assert evt["payload"]["worker_id"] == "w-001"

        # worker.teardown
        evt2 = event_worker_teardown(
            worker_id="w-001", job_id="j-001",
            total_egress_requests=5, denied_egress_requests=2,
            duration_seconds=12.5,
        )
        assert evt2["event_type"] == "worker.teardown"
        assert evt2["payload"]["total_egress_requests"] == 5
        assert evt2["payload"]["denied_egress_requests"] == 2

        # worker_preparation_failed
        evt3 = event_worker_preparation_failed(
            job_id="j-002", capability_id="route.bad",
            error="unknown capability",
        )
        assert evt3["event_type"] == "job.failed"
        assert evt3["payload"]["error_class"] == "worker_preparation_failed"

    def test_egress_policy_store_load_from_routes(self):
        """EgressPolicyStore.load_from_routes builds correct policies."""
        store = EgressPolicyStore()
        store.load_from_routes([
            {
                "endpoint_url": "https://api.anthropic.com/v1/messages",
                "allowed_capabilities": ["route.cloud.general"],
                "rate_limit_rpm": 60,
            },
            {
                "endpoint_url": "https://api.openai.com/v1/chat/completions",
                "allowed_capabilities": ["route.cloud.general", "route.cloud.openai"],
                "rate_limit_rpm": 30,
            },
        ])

        policy = store.get("route.cloud.general")
        assert policy is not None
        assert "api.anthropic.com:443" in policy.allowed_endpoints
        assert "api.openai.com:443" in policy.allowed_endpoints
        assert policy.rate_limit_rpm == 60  # max of 60 and 30

        policy2 = store.get("route.cloud.openai")
        assert policy2 is not None
        assert "api.openai.com:443" in policy2.allowed_endpoints
        assert policy2.rate_limit_rpm == 30

    @pytest.mark.asyncio
    async def test_default_deny_for_local_worker_egress(self, tmp_path):
        """A local worker with no egress_allow denies all non-Ollama traffic."""
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.local")
        ctx = await lifecycle.prepare_worker(job)

        # Anything except Ollama should be denied
        d1 = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
        assert d1.allowed is False
        assert d1.reason == "network_mode_none"

        # local worker has network_mode "none" so even Ollama is blocked
        d2 = ctx.egress_proxy.authorize("127.0.0.1", 11434, "POST")
        assert d2.allowed is False  # network_mode=none blocks everything

    @pytest.mark.asyncio
    async def test_lifecycle_wires_rate_limiter_into_proxy(self, tmp_path):
        """prepare_worker() creates EgressProxy with rate limiter wired in.

        Verifies the full chain: WorkerLifecycle → EgressProxy → authorize()
        enforces rate limiting on cloud endpoints.
        """
        lifecycle, audit, _, _ = _make_lifecycle(str(tmp_path))
        job = _make_job("route.cloud.general", job_id="rl-wiring-001")
        ctx = await lifecycle.prepare_worker(job)

        # The proxy should have a rate limiter (policy.rate_limit_rpm=30 > 0)
        assert ctx.egress_proxy._rate_limiter is not None

        # Burn through the rate limit (30 rpm from _make_egress_policy_store)
        for _ in range(30):
            d = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
            assert d.allowed is True

        # 31st request should be rate limited
        d = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
        assert d.allowed is False
        assert d.reason == "rate_limited"
