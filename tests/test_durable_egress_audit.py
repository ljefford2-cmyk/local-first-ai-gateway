"""Tests for durable egress audit wiring (Priority #4 — Gap Closure).

Verifies that egress events (authorized and denied) reach the persistent
audit log via AuditLogClient, closing the "durability trails governance
language" finding from the four-model adversarial review.

The gap: EgressProxy.log_request() appended to an in-memory list only.
The fix: EgressProxy.log_request_durable() also sends through AuditLogClient.
WorkerLifecycle now wires its audit_client into the EgressProxy it creates.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from runtime_manifest import (
    NetworkPolicy,
    ResourceLimits,
    RuntimeManifest,
    SecurityPolicy,
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
from worker_lifecycle import EgressPolicyStore, WorkerLifecycle, WorkerPreparationError
from models import Job, JobStatus
from test_helpers import MockAuditClient

# UUIDv7 pattern
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

MINIMAL_CAPS = {
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
            "1": {}, "2": {}, "3": {},
        },
        "promotion_criteria": {"0_to_1": None, "1_to_2": None, "2_to_3": None},
    },
}

TEST_EGRESS_ROUTES = [
    {
        "endpoint_url": "https://api.anthropic.com/v1/messages",
        "allowed_capabilities": ["route.cloud.claude"],
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


def _make_validator():
    reg = _make_registry()
    sm = _make_state(reg)
    endpoints = build_egress_endpoints_from_routes(TEST_EGRESS_ROUTES)
    v = ManifestValidator(reg, sm, egress_endpoints=endpoints)
    return v, reg, sm


def _valid_cloud_manifest():
    return RuntimeManifest(
        capability_id="route.cloud.claude",
        worker_type="cloud_adapter",
        volumes=[VolumeMount(path="/inbox", mode="ro", host_path="/data/inbox")],
        network=NetworkPolicy(
            egress_allow=["api.anthropic.com:443"],
            egress_deny_all=True,
            ingress="none",
        ),
        resources=ResourceLimits(max_memory_mb=256, max_cpu_seconds=30, max_wall_seconds=120, max_disk_mb=50),
        security=SecurityPolicy(drop_capabilities=["ALL"], read_only_root=True, no_new_privileges=True, seccomp_profile="default"),
    )


def _make_cloud_proxy(audit_client=None):
    """Build an EgressProxy for a cloud worker, optionally with audit client."""
    v, _, _ = _make_validator()
    m = _valid_cloud_manifest()
    engine = BlueprintEngine()
    result = v.validate(m)
    assert result.valid
    bp = engine.generate(m, result)
    policy = EgressPolicy(
        capability_id="route.cloud.claude",
        allowed_endpoints=["api.anthropic.com:443"],
        rate_limit_rpm=30,
    )
    return EgressProxy(
        blueprint=bp, egress_policy=policy, audit_client=audit_client
    )


def _assert_valid_event(evt, *, event_type):
    """Validate event envelope structure."""
    assert evt["schema_version"] == "2.0"
    assert UUID_V7_RE.match(evt["source_event_id"])
    assert evt["event_type"] == event_type
    assert evt["source"] == "egress_proxy"
    assert isinstance(evt["payload"], dict)


# =============================================================================
# 1. log_request_durable — audit client wired
# =============================================================================


class TestDurableEgressWithAuditClient:
    """Egress events reach the persistent audit log when audit_client is set."""

    def test_authorized_event_sent_to_audit_client(self):
        """An allowed egress decision sends egress.authorized to the audit client."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is True

        asyncio.run(
            proxy.log_request_durable("api.anthropic.com", 443, decision)
        )

        # Event reached the audit client
        assert len(mock.events) == 1
        evt = mock.events[0]
        _assert_valid_event(evt, event_type="egress.authorized")
        assert evt["payload"]["target_host"] == "api.anthropic.com"
        assert evt["payload"]["target_port"] == 443
        assert evt["payload"]["matched_rule"] == "api.anthropic.com:443"

    def test_denied_event_sent_to_audit_client(self):
        """A denied egress decision sends egress.denied to the audit client."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        decision = proxy.authorize("evil.example.com", 443, "POST")
        assert decision.allowed is False

        asyncio.run(
            proxy.log_request_durable("evil.example.com", 443, decision)
        )

        assert len(mock.events) == 1
        evt = mock.events[0]
        _assert_valid_event(evt, event_type="egress.denied")
        assert evt["payload"]["target_host"] == "evil.example.com"
        assert evt["payload"]["denial_reason"] == "default_deny"

    def test_durable_also_populates_in_memory_list(self):
        """log_request_durable writes to both the audit client AND the in-memory list."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        asyncio.run(
            proxy.log_request_durable("api.anthropic.com", 443, decision)
        )

        # In-memory list is also updated (used by teardown_worker for counts)
        assert len(proxy.events) == 1
        assert proxy.events[0]["event_type"] == "egress.authorized"

        # Audit client also has the event
        assert len(mock.events) == 1

    def test_multiple_events_all_reach_audit_client(self):
        """Multiple egress decisions all reach the persistent log."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        # 2 allowed, 1 denied
        for host, expected_allowed in [
            ("api.anthropic.com", True),
            ("api.anthropic.com", True),
            ("evil.example.com", False),
        ]:
            decision = proxy.authorize(host, 443, "POST")
            assert decision.allowed is expected_allowed
            asyncio.run(
                proxy.log_request_durable(host, 443, decision)
            )

        assert len(mock.events) == 3
        assert len(proxy.events) == 3
        types = [e["event_type"] for e in mock.events]
        assert types == ["egress.authorized", "egress.authorized", "egress.denied"]

    def test_audit_client_property_exposed(self):
        """EgressProxy.audit_client returns the configured client."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)
        assert proxy.audit_client is mock


# =============================================================================
# 2. log_request_durable — no audit client (graceful fallback)
# =============================================================================


class TestDurableEgressWithoutAuditClient:
    """Without audit_client, log_request_durable degrades to in-memory only."""

    def test_no_audit_client_still_populates_in_memory(self):
        """Without audit client, events still go to the in-memory list."""
        proxy = _make_cloud_proxy(audit_client=None)

        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        asyncio.run(
            proxy.log_request_durable("api.anthropic.com", 443, decision)
        )

        assert len(proxy.events) == 1
        assert proxy.events[0]["event_type"] == "egress.authorized"

    def test_no_audit_client_property_is_none(self):
        """EgressProxy.audit_client is None when not configured."""
        proxy = _make_cloud_proxy(audit_client=None)
        assert proxy.audit_client is None


# =============================================================================
# 3. Backward compatibility — sync log_request unchanged
# =============================================================================


class TestSyncLogRequestUnchanged:
    """The original sync log_request() still works exactly as before."""

    def test_sync_log_request_in_memory_only(self):
        """sync log_request still works and does NOT attempt audit client emit."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        proxy.log_request("api.anthropic.com", 443, decision)

        # In-memory list updated
        assert len(proxy.events) == 1

        # Audit client NOT called — sync path is unchanged
        assert len(mock.events) == 0

    def test_sync_log_request_without_audit_client(self):
        """sync log_request works without audit_client (full backward compat)."""
        proxy = _make_cloud_proxy(audit_client=None)

        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        proxy.log_request("api.anthropic.com", 443, decision)

        assert len(proxy.events) == 1


# =============================================================================
# 4. WorkerLifecycle wires audit_client into EgressProxy
# =============================================================================


class TestWorkerLifecycleAuditWiring:
    """WorkerLifecycle.prepare_worker() passes its audit_client to EgressProxy."""

    def _make_lifecycle(self):
        """Build a WorkerLifecycle with a mock audit client."""
        reg = _make_registry()
        sm = _make_state(reg)
        endpoints = build_egress_endpoints_from_routes(TEST_EGRESS_ROUTES)
        validator = ManifestValidator(reg, sm, egress_endpoints=endpoints)
        engine = BlueprintEngine()
        egress_store = EgressPolicyStore()
        egress_store.load_from_routes(TEST_EGRESS_ROUTES)
        mock = MockAuditClient()
        lifecycle = WorkerLifecycle(
            manifest_validator=validator,
            blueprint_engine=engine,
            capability_registry=reg,
            egress_policy_store=egress_store,
            audit_client=mock,
            state_manager=sm,
        )
        return lifecycle, mock

    def test_prepared_worker_has_audit_client_on_proxy(self):
        """After prepare_worker(), the EgressProxy has the audit client."""
        lifecycle, mock = self._make_lifecycle()
        job = Job(
            job_id="test-job-1",
            raw_input="test",
            input_modality="text",
            device="terminal",
            status=JobStatus.submitted,
            governing_capability_id="route.cloud.claude",
        )
        ctx = asyncio.run(
            lifecycle.prepare_worker(job)
        )

        assert ctx.egress_proxy is not None
        assert ctx.egress_proxy.audit_client is mock

    def test_egress_events_durable_through_lifecycle(self):
        """Egress events in a lifecycle-managed proxy reach the audit client."""
        lifecycle, mock = self._make_lifecycle()
        job = Job(
            job_id="test-job-2",
            raw_input="test",
            input_modality="text",
            device="terminal",
            status=JobStatus.submitted,
            governing_capability_id="route.cloud.claude",
        )
        ctx = asyncio.run(
            lifecycle.prepare_worker(job)
        )

        # Clear the lifecycle setup events so we can isolate egress events
        lifecycle_event_count = len(mock.events)

        # Simulate an egress check + durable log
        decision = ctx.egress_proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is True
        asyncio.run(
            ctx.egress_proxy.log_request_durable("api.anthropic.com", 443, decision)
        )

        # New egress event reached the audit client
        new_events = mock.events[lifecycle_event_count:]
        assert len(new_events) == 1
        assert new_events[0]["event_type"] == "egress.authorized"

    def test_teardown_counts_include_durable_events(self):
        """Teardown worker correctly counts events logged through durable path."""
        lifecycle, mock = self._make_lifecycle()
        job = Job(
            job_id="test-job-3",
            raw_input="test",
            input_modality="text",
            device="terminal",
            status=JobStatus.submitted,
            governing_capability_id="route.cloud.claude",
        )
        ctx = asyncio.run(
            lifecycle.prepare_worker(job)
        )

        # Log 2 allowed + 1 denied via durable path
        for host, port in [
            ("api.anthropic.com", 443),
            ("api.anthropic.com", 443),
            ("evil.example.com", 443),
        ]:
            decision = ctx.egress_proxy.authorize(host, port, "POST")
            asyncio.run(
                ctx.egress_proxy.log_request_durable(host, port, decision)
            )

        # Teardown should see 3 total, 1 denied
        asyncio.run(
            lifecycle.teardown_worker(ctx)
        )

        # Find the teardown event in the audit client
        teardown_events = mock.get_events_by_type("worker.teardown")
        assert len(teardown_events) == 1
        payload = teardown_events[0]["payload"]
        assert payload["total_egress_requests"] == 3
        assert payload["denied_egress_requests"] == 1


# =============================================================================
# 5. Event envelope integrity on durable path
# =============================================================================


class TestDurableEventEnvelopeIntegrity:
    """Events sent through the durable path have correct envelope fields."""

    def test_authorized_envelope_fields(self):
        """egress.authorized event has all required envelope and payload fields."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        asyncio.run(
            proxy.log_request_durable("api.anthropic.com", 443, decision)
        )

        evt = mock.events[0]
        # Envelope
        assert evt["schema_version"] == "2.0"
        assert UUID_V7_RE.match(evt["source_event_id"])
        assert evt["event_type"] == "egress.authorized"
        assert evt["source"] == "egress_proxy"
        assert evt["durability"] == "durable"
        # Payload
        p = evt["payload"]
        assert p["blueprint_id"] == proxy.blueprint.blueprint_id
        assert p["capability_id"] == "route.cloud.claude"
        assert p["target_host"] == "api.anthropic.com"
        assert p["target_port"] == 443
        assert p["method"] == "egress_check"
        assert p["matched_rule"] == "api.anthropic.com:443"

    def test_denied_envelope_fields(self):
        """egress.denied event has all required envelope and payload fields."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        decision = proxy.authorize("evil.example.com", 443, "POST")
        asyncio.run(
            proxy.log_request_durable("evil.example.com", 443, decision)
        )

        evt = mock.events[0]
        assert evt["event_type"] == "egress.denied"
        assert evt["durability"] == "durable"
        p = evt["payload"]
        assert p["denial_reason"] == "default_deny"
        assert p["target_host"] == "evil.example.com"

    def test_each_event_gets_unique_source_event_id(self):
        """Every durable event has a unique source_event_id (no dedup collision)."""
        mock = MockAuditClient()
        proxy = _make_cloud_proxy(audit_client=mock)

        for _ in range(5):
            decision = proxy.authorize("api.anthropic.com", 443, "POST")
            asyncio.run(
                proxy.log_request_durable("api.anthropic.com", 443, decision)
            )

        ids = [e["source_event_id"] for e in mock.events]
        assert len(set(ids)) == 5, f"Expected 5 unique IDs, got {len(set(ids))}"
