"""Phase 6C unit tests — Egress Proxy, Network Isolation, Rate Limiting.

Target: 18+ tests covering authorization rules, audit events,
per-worker rate limiting, and integration with prior phases.
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
from blueprint_engine import BlueprintEngine
from egress_proxy import EgressProxy, EgressDecision, EgressPolicy
from egress_events import event_egress_authorized, event_egress_denied
from egress_rate_limiter import EgressRateLimiter
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager


# UUIDv7 pattern for event validation
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
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
            "1": {}, "2": {}, "3": {},
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
        resources=ResourceLimits(max_memory_mb=256, max_cpu_seconds=30, max_wall_seconds=120, max_disk_mb=50),
        security=SecurityPolicy(drop_capabilities=["ALL"], read_only_root=True, no_new_privileges=True, seccomp_profile="default"),
    )
    kwargs.update(overrides)
    return RuntimeManifest(**kwargs)


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
        resources=ResourceLimits(max_memory_mb=256, max_cpu_seconds=60, max_wall_seconds=120, max_disk_mb=100),
        security=SecurityPolicy(drop_capabilities=["ALL"], read_only_root=True, no_new_privileges=True, seccomp_profile="default"),
    )
    kwargs.update(overrides)
    return RuntimeManifest(**kwargs)


def _make_blueprint(manifest, validator):
    """Validate manifest and generate blueprint."""
    engine = BlueprintEngine()
    result = validator.validate(manifest)
    assert result.valid, f"Manifest invalid: {result.errors}"
    return engine.generate(manifest, result)


def _make_cloud_proxy(rate_limit_rpm=30):
    """Build an EgressProxy for a cloud worker with Anthropic endpoint."""
    v, _, _ = _make_validator()
    m = _valid_cloud_manifest()
    bp = _make_blueprint(m, v)
    policy = EgressPolicy(
        capability_id="route.cloud.claude",
        allowed_endpoints=["api.anthropic.com:443"],
        rate_limit_rpm=rate_limit_rpm,
    )
    return EgressProxy(blueprint=bp, egress_policy=policy)


def _make_local_proxy():
    """Build an EgressProxy for a local Ollama worker."""
    v, _, _ = _make_validator()
    m = _valid_local_manifest()
    bp = _make_blueprint(m, v)
    policy = EgressPolicy(
        capability_id="route.local",
        allowed_endpoints=["ollama:11434"],
        rate_limit_rpm=120,
    )
    return EgressProxy(blueprint=bp, egress_policy=policy, ollama_host="ollama", ollama_port=11434)


def _make_no_network_proxy():
    """Build an EgressProxy with network_mode 'none'."""
    v, _, _ = _make_validator()
    m = _valid_local_manifest(
        network=NetworkPolicy(egress_allow=[], egress_deny_all=True, ingress="none"),
    )
    bp = _make_blueprint(m, v)
    policy = EgressPolicy(
        capability_id="route.local",
        allowed_endpoints=[],
        rate_limit_rpm=30,
    )
    return EgressProxy(blueprint=bp, egress_policy=policy)


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
# a. Authorization Happy Path Tests (3+)
# =============================================================================

class TestAuthorizationHappyPath:
    def test_allowed_endpoint_passes(self):
        """An endpoint in both blueprint allowlist and Spec 4 policy is allowed."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is True
        assert decision.reason == "allowlist_match"
        assert decision.matched_rule == "api.anthropic.com:443"

    def test_ollama_always_passes(self):
        """Local Ollama endpoint is always reachable regardless of allowlist."""
        proxy = _make_local_proxy()
        decision = proxy.authorize("ollama", 11434, "POST")
        assert decision.allowed is True
        assert decision.reason == "ollama_always_allowed"
        assert "ollama" in decision.matched_rule

    def test_correct_matched_rule_returned(self):
        """The matched_rule field contains the exact endpoint string that matched."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("api.anthropic.com", 443, "GET")
        assert decision.matched_rule == "api.anthropic.com:443"

    def test_ollama_default_host_always_passes(self):
        """Default Ollama (127.0.0.1:11434) is always reachable."""
        proxy = _make_cloud_proxy()  # Uses default ollama host 127.0.0.1:11434
        decision = proxy.authorize("127.0.0.1", 11434, "POST")
        assert decision.allowed is True
        assert decision.reason == "ollama_always_allowed"


# =============================================================================
# b. Authorization Denial Tests (5+)
# =============================================================================

class TestAuthorizationDenial:
    def test_network_mode_none_denies_all(self):
        """network_mode 'none' denies ALL requests — no exceptions."""
        proxy = _make_no_network_proxy()
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is False
        assert decision.reason == "network_mode_none"

    def test_network_mode_none_denies_even_ollama(self):
        """network_mode 'none' denies even the default Ollama endpoint."""
        proxy = _make_no_network_proxy()
        decision = proxy.authorize("127.0.0.1", 11434, "POST")
        assert decision.allowed is False
        assert decision.reason == "network_mode_none"

    def test_endpoint_not_in_allowlist_denied(self):
        """An endpoint not in the blueprint allowlist is denied (default_deny)."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("evil.example.com", 443, "POST")
        assert decision.allowed is False
        assert decision.reason == "default_deny"

    def test_manifest_allows_but_policy_denies(self):
        """Endpoint in blueprint but not in Spec 4 policy → manifest_allows_but_policy_denies."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()
        bp = _make_blueprint(m, v)
        # Policy does NOT include api.anthropic.com:443
        policy = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=[],  # Empty policy — nothing authorized
            rate_limit_rpm=30,
        )
        proxy = EgressProxy(blueprint=bp, egress_policy=policy)
        # Blueprint has api.anthropic.com:443, but policy doesn't
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is False
        assert decision.reason == "manifest_allows_but_policy_denies"

    def test_default_deny_for_unknown_endpoint(self):
        """A completely unknown endpoint gets default_deny."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("unknown-service.internal", 8080, "GET")
        assert decision.allowed is False
        assert decision.reason == "default_deny"
        assert decision.matched_rule is None

    def test_rate_limited_request_denied(self):
        """When rate limit is exceeded, subsequent requests are denied."""
        proxy = _make_cloud_proxy(rate_limit_rpm=2)
        limiter = EgressRateLimiter()
        bp_id = proxy.blueprint.blueprint_id

        # Use up the rate limit
        assert limiter.check(bp_id, 2) is True
        assert limiter.check(bp_id, 2) is True
        # Third request should be denied
        assert limiter.check(bp_id, 2) is False


# =============================================================================
# c. Audit Event Tests (4+)
# =============================================================================

class TestAuditEvents:
    def test_egress_authorized_event_correct(self):
        """egress.authorized event has correct envelope and payload."""
        evt = event_egress_authorized(
            blueprint_id="bp-001",
            capability_id="route.cloud.claude",
            target_host="api.anthropic.com",
            target_port=443,
            method="POST",
            matched_rule="api.anthropic.com:443",
        )
        _assert_valid_envelope(evt, event_type="egress.authorized", source="egress_proxy")
        p = evt["payload"]
        assert p["blueprint_id"] == "bp-001"
        assert p["capability_id"] == "route.cloud.claude"
        assert p["target_host"] == "api.anthropic.com"
        assert p["target_port"] == 443
        assert p["method"] == "POST"
        assert p["matched_rule"] == "api.anthropic.com:443"

    def test_egress_denied_event_correct(self):
        """egress.denied event has correct envelope and payload."""
        evt = event_egress_denied(
            blueprint_id="bp-002",
            capability_id="route.local",
            target_host="evil.example.com",
            target_port=443,
            method="GET",
            denial_reason="default_deny",
        )
        _assert_valid_envelope(evt, event_type="egress.denied", source="egress_proxy")
        p = evt["payload"]
        assert p["blueprint_id"] == "bp-002"
        assert p["capability_id"] == "route.local"
        assert p["target_host"] == "evil.example.com"
        assert p["target_port"] == 443
        assert p["denial_reason"] == "default_deny"

    def test_allowed_request_produces_event(self):
        """An allowed request produces an egress.authorized event via log_request."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        proxy.log_request("api.anthropic.com", 443, decision)
        events = proxy.events
        assert len(events) == 1
        assert events[0]["event_type"] == "egress.authorized"
        assert events[0]["payload"]["matched_rule"] == "api.anthropic.com:443"

    def test_denied_request_produces_event(self):
        """A denied request produces an egress.denied event via log_request."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("evil.example.com", 443, "POST")
        proxy.log_request("evil.example.com", 443, decision)
        events = proxy.events
        assert len(events) == 1
        assert events[0]["event_type"] == "egress.denied"
        assert events[0]["payload"]["denial_reason"] == "default_deny"

    def test_every_request_produces_event(self):
        """Both allowed and denied requests produce events — nothing is silent."""
        proxy = _make_cloud_proxy()

        # Allowed request
        d1 = proxy.authorize("api.anthropic.com", 443, "POST")
        proxy.log_request("api.anthropic.com", 443, d1)

        # Denied request
        d2 = proxy.authorize("evil.example.com", 443, "POST")
        proxy.log_request("evil.example.com", 443, d2)

        events = proxy.events
        assert len(events) == 2
        assert events[0]["event_type"] == "egress.authorized"
        assert events[1]["event_type"] == "egress.denied"

    def test_events_follow_build_event_pattern(self):
        """Events have schema_version, UUIDv7 source_event_id, timestamps."""
        proxy = _make_cloud_proxy()
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        proxy.log_request("api.anthropic.com", 443, decision)
        evt = proxy.events[0]
        assert evt["schema_version"] == "2.0"
        assert UUID_V7_RE.match(evt["source_event_id"])
        assert evt["source"] == "egress_proxy"


# =============================================================================
# d. Rate Limiting Tests (3+)
# =============================================================================

class TestRateLimiting:
    def test_under_limit_passes(self):
        """Requests under the rate limit are allowed."""
        limiter = EgressRateLimiter()
        assert limiter.check("bp-100", 5) is True
        assert limiter.current_count("bp-100") == 1

    def test_at_limit_last_request_passes(self):
        """The request that reaches exactly the limit is allowed."""
        limiter = EgressRateLimiter()
        for _ in range(4):
            limiter.check("bp-200", 5)
        # 5th request (at limit) should pass
        assert limiter.check("bp-200", 5) is True
        assert limiter.current_count("bp-200") == 5

    def test_over_limit_denied(self):
        """Requests over the rate limit are denied."""
        limiter = EgressRateLimiter()
        for _ in range(5):
            assert limiter.check("bp-300", 5) is True
        # 6th request should be denied
        assert limiter.check("bp-300", 5) is False
        assert limiter.current_count("bp-300") == 5

    def test_different_workers_independent_counters(self):
        """Worker A's request count does not affect Worker B."""
        limiter = EgressRateLimiter()

        # Worker A uses up its limit
        for _ in range(3):
            limiter.check("worker-A", 3)
        assert limiter.check("worker-A", 3) is False  # A is rate limited

        # Worker B should be unaffected
        assert limiter.check("worker-B", 3) is True
        assert limiter.current_count("worker-B") == 1

    def test_reset_clears_counter(self):
        """Resetting a worker's counter allows new requests."""
        limiter = EgressRateLimiter()
        for _ in range(3):
            limiter.check("bp-400", 3)
        assert limiter.check("bp-400", 3) is False
        limiter.reset("bp-400")
        assert limiter.check("bp-400", 3) is True


# =============================================================================
# e. Integration Tests (3+)
# =============================================================================

class TestIntegration:
    def test_full_chain_manifest_to_authorize(self):
        """Full chain: manifest → validate → blueprint → egress proxy → authorize."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()

        # Validate manifest
        result = v.validate(m)
        assert result.valid is True

        # Generate blueprint
        engine = BlueprintEngine()
        bp = engine.generate(m, result)

        # Build egress policy from Spec 4 routes
        endpoints = build_egress_endpoints_from_routes(TEST_EGRESS_ROUTES)
        policy = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=endpoints.get("route.cloud.claude", []),
            rate_limit_rpm=30,
        )

        # Create proxy and authorize
        proxy = EgressProxy(blueprint=bp, egress_policy=policy)
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is True
        assert decision.matched_rule == "api.anthropic.com:443"

        # Unknown endpoint denied
        decision2 = proxy.authorize("evil.example.com", 443, "POST")
        assert decision2.allowed is False

    def test_capability_demotion_causes_egress_policy_change(self):
        """Capability demotion in Spec 2 causes egress policy change → proxy reflects it.

        When a capability is demoted/suspended, re-validation fails, and no new
        blueprint (hence no new proxy) can be created. Existing proxy continues
        to enforce its current rules — the demotion prevents NEW proxies.
        """
        v, reg, sm = _make_validator()
        m = _valid_cloud_manifest()

        # Initially valid — can create proxy
        result = v.validate(m)
        assert result.valid is True
        engine = BlueprintEngine()
        bp = engine.generate(m, result)
        policy = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=30,
        )
        proxy = EgressProxy(blueprint=bp, egress_policy=policy)
        assert proxy.authorize("api.anthropic.com", 443, "POST").allowed is True

        # Suspend capability — re-validation should fail
        sm.suspend("route.cloud.claude")
        result2 = v.validate(m)
        assert result2.valid is False

        # Cannot create a new blueprint/proxy for the suspended capability
        try:
            engine.generate(m, result2)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass

    def test_override_redirect_new_capability_new_proxy(self):
        """Override redirect to new capability → new proxy with new rules.

        When a job is redirected (Spec 5) to a different capability,
        a new manifest/blueprint/proxy is created with the new capability's
        egress rules.
        """
        v, _, _ = _make_validator()

        # Original: cloud worker with Anthropic access
        m_cloud = _valid_cloud_manifest()
        bp_cloud = _make_blueprint(m_cloud, v)
        policy_cloud = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=30,
        )
        proxy_cloud = EgressProxy(blueprint=bp_cloud, egress_policy=policy_cloud)
        assert proxy_cloud.authorize("api.anthropic.com", 443, "POST").allowed is True
        assert proxy_cloud.authorize("ollama", 11434, "POST").allowed is False  # Not configured as Ollama host

        # Redirect to local capability — different egress rules
        m_local = _valid_local_manifest()
        bp_local = _make_blueprint(m_local, v)
        policy_local = EgressPolicy(
            capability_id="route.local",
            allowed_endpoints=["ollama:11434"],
            rate_limit_rpm=120,
        )
        proxy_local = EgressProxy(
            blueprint=bp_local, egress_policy=policy_local,
            ollama_host="ollama", ollama_port=11434,
        )
        # Local proxy allows Ollama
        assert proxy_local.authorize("ollama", 11434, "POST").allowed is True
        # Local proxy denies Anthropic
        assert proxy_local.authorize("api.anthropic.com", 443, "POST").allowed is False

    def test_blueprint_carries_egress_allow(self):
        """Blueprint's network_config.egress_allow is populated from manifest."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()
        bp = _make_blueprint(m, v)
        assert bp.network_config.egress_allow == ["api.anthropic.com:443"]
        assert bp.network_config.network_mode == "drnt-internal"


# =============================================================================
# f. Inline Rate Limiting Tests (5)
# =============================================================================

class TestInlineRateLimiting:
    """Tests that EgressProxy.authorize() enforces rate limits inline."""

    def test_allowlisted_request_denied_when_over_rate_limit(self):
        """A request passing the allowlist but exceeding rate limit is DENIED."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()
        bp = _make_blueprint(m, v)
        policy = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=2,
        )
        limiter = EgressRateLimiter()
        proxy = EgressProxy(blueprint=bp, egress_policy=policy, rate_limiter=limiter)

        # First two requests pass
        assert proxy.authorize("api.anthropic.com", 443, "POST").allowed is True
        assert proxy.authorize("api.anthropic.com", 443, "POST").allowed is True

        # Third request is rate limited
        decision = proxy.authorize("api.anthropic.com", 443, "POST")
        assert decision.allowed is False
        assert decision.reason == "rate_limited"
        assert decision.matched_rule is None

    def test_ollama_not_rate_limited(self):
        """Ollama requests bypass rate limiting even when limiter is active."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()
        bp = _make_blueprint(m, v)
        policy = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=1,
        )
        limiter = EgressRateLimiter()
        proxy = EgressProxy(blueprint=bp, egress_policy=policy, rate_limiter=limiter)

        # Exhaust the rate limit on a cloud endpoint
        proxy.authorize("api.anthropic.com", 443, "POST")
        assert proxy.authorize("api.anthropic.com", 443, "POST").allowed is False

        # Ollama still passes — it's Rule 2, before rate limiting
        for _ in range(10):
            decision = proxy.authorize("127.0.0.1", 11434, "POST")
            assert decision.allowed is True
            assert decision.reason == "ollama_always_allowed"

    def test_under_rate_limit_passes_normally(self):
        """Requests under the rate limit pass through authorize() normally."""
        v, _, _ = _make_validator()
        m = _valid_cloud_manifest()
        bp = _make_blueprint(m, v)
        policy = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=100,
        )
        limiter = EgressRateLimiter()
        proxy = EgressProxy(blueprint=bp, egress_policy=policy, rate_limiter=limiter)

        for _ in range(10):
            decision = proxy.authorize("api.anthropic.com", 443, "POST")
            assert decision.allowed is True
            assert decision.reason == "allowlist_match"

    def test_no_rate_limiter_backward_compatible(self):
        """EgressProxy without rate_limiter works identically to before."""
        proxy = _make_cloud_proxy()  # No rate limiter passed
        # Many requests should all pass — no rate limiting
        for _ in range(50):
            decision = proxy.authorize("api.anthropic.com", 443, "POST")
            assert decision.allowed is True

    def test_rate_limiter_scoped_per_blueprint(self):
        """Rate limiter uses blueprint_id as scoping key — different proxies are independent."""
        limiter = EgressRateLimiter()

        v, _, _ = _make_validator()
        m1 = _valid_cloud_manifest()
        bp1 = _make_blueprint(m1, v)
        policy1 = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=2,
        )
        proxy1 = EgressProxy(blueprint=bp1, egress_policy=policy1, rate_limiter=limiter)

        m2 = _valid_cloud_manifest()
        bp2 = _make_blueprint(m2, v)
        policy2 = EgressPolicy(
            capability_id="route.cloud.claude",
            allowed_endpoints=["api.anthropic.com:443"],
            rate_limit_rpm=2,
        )
        proxy2 = EgressProxy(blueprint=bp2, egress_policy=policy2, rate_limiter=limiter)

        # Exhaust proxy1's rate limit
        proxy1.authorize("api.anthropic.com", 443, "POST")
        proxy1.authorize("api.anthropic.com", 443, "POST")
        assert proxy1.authorize("api.anthropic.com", 443, "POST").allowed is False

        # proxy2 is unaffected (different blueprint_id)
        assert proxy2.authorize("api.anthropic.com", 443, "POST").allowed is True
