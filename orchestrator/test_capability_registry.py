"""Tests for capability_registry.py and capability_state.py."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from capability_registry import (
    ALL_ACTIONS,
    AUXILIARY_ACTIONS,
    GOVERNING_ACTIONS,
    CapabilityRegistry,
)
from capability_state import (
    EVALUABLE_OUTCOMES_MAX,
    CapabilityStateManager,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "capabilities.json")


def _load_default_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(config_path=CONFIG_PATH)
    reg.load()
    return reg


def _write_temp_config(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.write(fd, json.dumps(data).encode())
    os.close(fd)
    return path


def _minimal_cap(overrides: dict | None = None) -> dict:
    """Return a minimal valid capability dict for testing."""
    base = {
        "capability_id": "test.cap",
        "capability_name": "Test Capability",
        "capability_type": "auxiliary",
        "desired_wal_level": 0,
        "max_wal": 3,
        "declared_pipeline": [],
        "provider_dependencies": None,
        "action_policies": {
            "0": {"read_memory": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "1": {"read_memory": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "2": {"read_memory": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
            "3": {"read_memory": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
        },
        "promotion_criteria": {"0_to_1": None, "1_to_2": None, "2_to_3": None},
    }
    if overrides:
        base.update(overrides)
    return base


def _state_manager_in_tmp(registry: CapabilityRegistry) -> CapabilityStateManager:
    fd, path = tempfile.mkstemp(suffix=".state.json")
    os.close(fd)
    mgr = CapabilityStateManager(state_path=path)
    mgr.initialize_from_registry(registry)
    return mgr


# ---------------------------------------------------------------------------
# Registry Tests
# ---------------------------------------------------------------------------


class TestLoadDefaultConfig:
    def test_load_default_config(self):
        reg = _load_default_registry()
        assert len(reg.get_all()) == 12
        for cap_id, cap in reg.get_all().items():
            assert cap["capability_type"] in {"governing", "auxiliary", "operational"}
            assert cap["desired_wal_level"] <= cap["max_wal"]
            assert "capability_id" in cap
            assert "capability_name" in cap
            assert "declared_pipeline" in cap
            assert "provider_dependencies" in cap
            assert "action_policies" in cap
            assert "promotion_criteria" in cap

    def test_config_hash_computed(self):
        reg = _load_default_registry()
        assert reg.config_hash is not None
        assert len(reg.config_hash) == 64
        assert all(c in "0123456789abcdef" for c in reg.config_hash)


class TestCapabilityLookups:
    def test_governing_capabilities(self):
        reg = _load_default_registry()
        expected = [
            "route.local",
            "route.cloud.claude",
            "route.cloud.openai",
            "route.cloud.gemini",
            "route.multi",
        ]
        assert sorted(reg.get_governing()) == sorted(expected)

    def test_auxiliary_capabilities(self):
        reg = _load_default_registry()
        expected = [
            "context.package",
            "memory.read",
            "memory.write",
            "notify.watch",
            "notify.phone",
            "job.queue",
        ]
        assert sorted(reg.get_auxiliary()) == sorted(expected)

    def test_exclusive_action_ownership(self):
        reg = _load_default_registry()
        gov_actions: set[str] = set()
        aux_actions: set[str] = set()
        for cid in reg.get_governing():
            gov_actions.update(reg.get_actions_for(cid))
        for cid in reg.get_auxiliary():
            aux_actions.update(reg.get_actions_for(cid))
        assert gov_actions <= GOVERNING_ACTIONS
        assert aux_actions <= AUXILIARY_ACTIONS
        assert gov_actions & aux_actions == set()

    def test_sentinel_check(self):
        reg = _load_default_registry()
        assert reg.is_sentinel("egress_config") is True
        assert reg.is_sentinel("egress_connectivity") is True
        assert reg.is_sentinel("worker_sandbox") is True
        assert reg.is_sentinel("route.cloud.claude") is False

    def test_unknown_capability_returns_none(self):
        reg = _load_default_registry()
        assert reg.get("nonexistent.cap") is None


# ---------------------------------------------------------------------------
# Validation Error Tests
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_action_enum_violation(self):
        cap = _minimal_cap()
        cap["action_policies"]["0"]["invalid_action"] = "blocked"
        path = _write_temp_config({"test.cap": cap})
        try:
            reg = CapabilityRegistry(config_path=path)
            with pytest.raises(ValueError, match="unknown action"):
                reg.load()
        finally:
            os.unlink(path)

    def test_exclusive_ownership_violation(self):
        """dispatch_cloud assigned to an auxiliary capability should fail."""
        cap = _minimal_cap()
        cap["action_policies"]["0"] = {
            "dispatch_cloud": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}},
        }
        cap["action_policies"]["1"] = {}
        cap["action_policies"]["2"] = {}
        cap["action_policies"]["3"] = {}
        path = _write_temp_config({"test.aux": cap})
        try:
            reg = CapabilityRegistry(config_path=path)
            with pytest.raises(ValueError, match="non-auxiliary"):
                reg.load()
        finally:
            os.unlink(path)

    def test_desired_exceeds_max_wal(self):
        cap = _minimal_cap({"desired_wal_level": 3, "max_wal": 2})
        path = _write_temp_config({"test.cap": cap})
        try:
            reg = CapabilityRegistry(config_path=path)
            with pytest.raises(ValueError, match="exceeds max_wal"):
                reg.load()
        finally:
            os.unlink(path)

    def test_broken_pipeline_reference(self):
        cap = _minimal_cap({
            "declared_pipeline": [{"capability_id": "context.nonexistent", "dependency_mode": "required"}],
        })
        path = _write_temp_config({"test.cap": cap})
        try:
            reg = CapabilityRegistry(config_path=path)
            with pytest.raises(ValueError, match="context.nonexistent"):
                reg.load()
        finally:
            os.unlink(path)

    def test_broken_provider_dependency(self):
        cap = _minimal_cap({
            "capability_type": "governing",
            "provider_dependencies": ["route.cloud.nonexistent"],
            "action_policies": {
                "0": {"dispatch_cloud": {"review_gate": "none", "cost_gate_usd": None, "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True}}},
                "1": {},
                "2": {},
                "3": {},
            },
        })
        path = _write_temp_config({"test.gov": cap})
        try:
            reg = CapabilityRegistry(config_path=path)
            with pytest.raises(ValueError, match="route.cloud.nonexistent"):
                reg.load()
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# State Manager Tests
# ---------------------------------------------------------------------------


class TestStateInitialization:
    def test_state_initialization(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        assert len(mgr.get_all()) == 12
        for cap_id, entry in mgr.get_all().items():
            assert entry["effective_wal_level"] == 0
            assert entry["status"] == "active"
            assert entry["counters"]["evaluable_outcomes"] == []
            assert entry["counters"]["recent_failures"] == []


class TestRingBuffer:
    def test_ring_buffer_max(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        cap_id = "route.cloud.claude"
        for i in range(201):
            mgr.record_outcome(cap_id, {
                "source_event_id": f"evt-{i:04d}",
                "timestamp": _iso_now(),
                "disposition": "accepted",
                "cost_usd": None,
            })
        outcomes = mgr.get_outcomes(cap_id)
        assert len(outcomes) == 200
        assert outcomes[0]["source_event_id"] == "evt-0001"
        assert outcomes[-1]["source_event_id"] == "evt-0200"

    def test_ring_buffer_order(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        cap_id = "route.local"
        for i in range(5):
            mgr.record_outcome(cap_id, {
                "source_event_id": f"evt-{i}",
                "timestamp": _iso_now(),
                "disposition": "accepted",
                "cost_usd": None,
            })
        outcomes = mgr.get_outcomes(cap_id)
        assert [o["source_event_id"] for o in outcomes] == [
            "evt-0", "evt-1", "evt-2", "evt-3", "evt-4"
        ]


class TestFailureDeque:
    def test_failure_deque_eviction(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        cap_id = "route.cloud.claude"
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="microseconds")
        for i in range(3):
            mgr.record_failure(cap_id, old, f"evt-old-{i}")
        mgr.evict_old_failures(cap_id)
        assert mgr.get_recent_failure_count(cap_id) == 0

    def test_failure_deque_partial_eviction(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        cap_id = "route.cloud.openai"
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="microseconds")
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="microseconds")
        mgr.record_failure(cap_id, old, "evt-old-1")
        mgr.record_failure(cap_id, old, "evt-old-2")
        mgr.record_failure(cap_id, recent, "evt-recent-1")
        assert mgr.get_recent_failure_count(cap_id) == 1

    def test_failure_deque_no_success_reset(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        cap_id = "route.local"
        now = _iso_now()
        mgr.record_failure(cap_id, now, "evt-f1")
        mgr.record_failure(cap_id, now, "evt-f2")
        mgr.record_outcome(cap_id, {
            "source_event_id": "evt-success",
            "timestamp": now,
            "disposition": "accepted",
            "cost_usd": None,
        })
        mgr.record_failure(cap_id, now, "evt-f3")
        assert mgr.get_recent_failure_count(cap_id) == 3


class TestCounterReset:
    def test_counter_reset(self):
        reg = _load_default_registry()
        mgr = _state_manager_in_tmp(reg)
        cap_id = "route.cloud.claude"
        now = _iso_now()

        mgr.record_outcome(cap_id, {
            "source_event_id": "evt-1",
            "timestamp": now,
            "disposition": "accepted",
            "cost_usd": 0.05,
        })
        mgr.record_failure(cap_id, now, "evt-f1")

        entry = mgr.get(cap_id)
        entry["counters"]["last_incident_source_event_id"] = "incident-42"
        mgr.save()

        mgr.reset_counters(cap_id)

        entry = mgr.get(cap_id)
        assert entry["counters"]["evaluable_outcomes"] == []
        assert entry["counters"]["recent_failures"] == []
        assert entry["counters"]["first_job_date"] is None
        assert entry["counters"]["last_reset"] is not None
        assert entry["counters"]["last_incident_source_event_id"] == "incident-42"


class TestStatePersistence:
    def test_state_persistence(self):
        reg = _load_default_registry()
        fd, path = tempfile.mkstemp(suffix=".state.json")
        os.close(fd)

        mgr1 = CapabilityStateManager(state_path=path)
        mgr1.initialize_from_registry(reg)
        mgr1.record_outcome("route.local", {
            "source_event_id": "evt-persist",
            "timestamp": _iso_now(),
            "disposition": "accepted",
            "cost_usd": None,
        })

        mgr2 = CapabilityStateManager(state_path=path)
        mgr2.load()
        outcomes = mgr2.get_outcomes("route.local")
        assert len(outcomes) == 1
        assert outcomes[0]["source_event_id"] == "evt-persist"

        os.unlink(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
