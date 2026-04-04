"""Tests for admin_routes.py — WAL lifecycle testing endpoints."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from demotion_engine import DemotionEngine
from promotion_monitor import PromotionMonitor

import admin_routes
from admin_routes import (
    RecordOutcomeRequest,
    ModelChangeRequest,
    SimulateOverrideRequest,
    record_outcome,
    trigger_model_change,
    simulate_override,
    get_capabilities,
)
from test_helpers import MockAuditClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "capabilities.json"
)
_CONTAINER_CONFIG = "/var/drnt/config/capabilities.json"
CONFIG_PATH = _REPO_CONFIG if os.path.exists(_REPO_CONFIG) else _CONTAINER_CONFIG


def _make_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(config_path=CONFIG_PATH)
    reg.load()
    return reg


def _make_state(registry: CapabilityRegistry, tmp_path: str) -> CapabilityStateManager:
    state_path = os.path.join(tmp_path, "capabilities.state.json")
    mgr = CapabilityStateManager(state_path=state_path)
    mgr.initialize_from_registry(registry)
    return mgr


def _days_ago_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="microseconds"
    )


@pytest.fixture
def setup(tmp_path):
    """Provide registry, state, audit, and inject dependencies into admin_routes."""
    registry = _make_registry()
    state = _make_state(registry, str(tmp_path))
    audit = MockAuditClient()
    engine = DemotionEngine(registry, state, audit)
    monitor = PromotionMonitor(registry, state, audit)

    admin_routes.init(registry, state, engine, audit, monitor)

    return registry, state, audit, engine, monitor


# ===========================================================================
# Record Outcome Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_record_outcome_accepted(setup):
    """Record an accepted outcome. Verify status, count, and score."""
    _registry, _state, _audit, _engine, _monitor = setup

    result = await record_outcome(RecordOutcomeRequest(
        capability_id="route.cloud.claude",
        disposition="accepted",
    ))

    assert result.status == "recorded"
    assert result.capability_id == "route.cloud.claude"
    assert result.disposition == "accepted"
    assert result.outcomes_count == 1
    assert result.approval_score == 1.0


@pytest.mark.asyncio
async def test_record_outcome_updates_score(setup):
    """Record 9 accepted and 1 rejected. Verify score = 0.9."""
    _registry, _state, _audit, _engine, _monitor = setup

    for _ in range(9):
        await record_outcome(RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="accepted",
        ))
    result = await record_outcome(RecordOutcomeRequest(
        capability_id="route.cloud.claude",
        disposition="rejected",
    ))

    assert result.outcomes_count == 10
    assert abs(result.approval_score - 0.9) < 1e-4


@pytest.mark.asyncio
async def test_record_outcome_sets_first_job_date(setup):
    """Record an outcome. Verify first_job_date is now set."""
    _registry, state, _audit, _engine, _monitor = setup

    entry = state.get("route.cloud.claude")
    assert entry["counters"]["first_job_date"] is None

    await record_outcome(RecordOutcomeRequest(
        capability_id="route.cloud.claude",
        disposition="accepted",
    ))

    entry = state.get("route.cloud.claude")
    assert entry["counters"]["first_job_date"] is not None


@pytest.mark.asyncio
async def test_record_outcome_promotion_check_not_ready(setup):
    """Record 5 outcomes. Promotion check should show ready: false."""
    _registry, _state, _audit, _engine, _monitor = setup

    for _ in range(5):
        result = await record_outcome(RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="accepted",
        ))

    assert result.promotion_check["ready"] is False


@pytest.mark.asyncio
async def test_record_outcome_unknown_capability(setup):
    """Try to record for a nonexistent capability. Verify 404."""
    _registry, _state, _audit, _engine, _monitor = setup

    with pytest.raises(Exception) as exc_info:
        await record_outcome(RecordOutcomeRequest(
            capability_id="nonexistent.cap",
            disposition="accepted",
        ))

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_record_outcome_invalid_disposition():
    """Try invalid disposition. Verify Pydantic validation error."""
    with pytest.raises(Exception):
        RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="invalid",
        )


# ===========================================================================
# Model Change Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_model_change_all_capabilities(setup):
    """Trigger model change with affected_capabilities=None. All non-meta demoted."""
    _registry, state, audit, _engine, _monitor = setup

    state.set_effective_wal("route.cloud.claude", 1)

    result = await trigger_model_change(ModelChangeRequest(
        affected_capabilities=None,
    ))

    assert result.status == "processed"
    assert state.get_effective_wal("route.cloud.claude") == 0

    # Verify system.model_change event in audit log
    model_events = [e for e in audit.events if e["event_type"] == "system.model_change"]
    assert len(model_events) == 1

    # Verify demotion listed in response
    claude_demotions = [d for d in result.demotions if d["capability_id"] == "route.cloud.claude"]
    assert len(claude_demotions) == 1
    assert claude_demotions[0]["from_level"] == 1
    assert claude_demotions[0]["to_level"] == 0


@pytest.mark.asyncio
async def test_model_change_specific_capabilities(setup):
    """Model change with specific list. Only listed capabilities demoted."""
    _registry, state, _audit, _engine, _monitor = setup

    state.set_effective_wal("route.cloud.claude", 1)
    state.set_effective_wal("route.cloud.openai", 1)

    result = await trigger_model_change(ModelChangeRequest(
        affected_capabilities=["route.cloud.claude"],
    ))

    assert state.get_effective_wal("route.cloud.claude") == 0
    assert state.get_effective_wal("route.cloud.openai") == 1

    assert len(result.demotions) == 1
    assert result.demotions[0]["capability_id"] == "route.cloud.claude"


@pytest.mark.asyncio
async def test_model_change_already_wal0(setup):
    """All at WAL-0. Trigger model change. No demotions."""
    _registry, _state, _audit, _engine, _monitor = setup

    result = await trigger_model_change(ModelChangeRequest(
        affected_capabilities=["route.cloud.claude"],
    ))

    assert result.status == "processed"
    assert len(result.demotions) == 0


@pytest.mark.asyncio
async def test_model_change_unknown_capability(setup):
    """Include nonexistent capability. Verify 400."""
    _registry, _state, _audit, _engine, _monitor = setup

    with pytest.raises(Exception) as exc_info:
        await trigger_model_change(ModelChangeRequest(
            affected_capabilities=["nonexistent.cap"],
        ))

    assert exc_info.value.status_code == 400


# ===========================================================================
# Simulate Override Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_override_cancel_demotes(setup):
    """Cancel override on WAL-1 capability demotes to WAL-0."""
    _registry, state, _audit, _engine, _monitor = setup

    state.set_effective_wal("route.cloud.claude", 1)

    result = await simulate_override(SimulateOverrideRequest(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
    ))

    assert result.status == "processed"
    assert result.demoted is True
    assert result.detail["from_level"] == 1
    assert result.detail["to_level"] == 0
    assert state.get_effective_wal("route.cloud.claude") == 0


@pytest.mark.asyncio
async def test_override_at_wal0_no_demotion(setup):
    """Override at WAL-0. No demotion."""
    _registry, state, _audit, _engine, _monitor = setup

    assert state.get_effective_wal("route.cloud.claude") == 0

    result = await simulate_override(SimulateOverrideRequest(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
    ))

    assert result.demoted is False


@pytest.mark.asyncio
async def test_override_sentinel_exemption(setup):
    """Cancel with prior sentinel failure. No demotion."""
    _registry, state, _audit, _engine, _monitor = setup

    state.set_effective_wal("route.cloud.claude", 1)

    result = await simulate_override(SimulateOverrideRequest(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
        prior_failure_capability_id="egress_connectivity",
    ))

    assert result.demoted is False
    assert "sentinel" in result.detail["reason"]


@pytest.mark.asyncio
async def test_override_redirect_demotes(setup):
    """Redirect override on WAL-2 demotes to WAL-1."""
    _registry, state, _audit, _engine, _monitor = setup

    state.set_effective_wal("route.cloud.claude", 2)

    result = await simulate_override(SimulateOverrideRequest(
        override_type="redirect",
        governing_capability_id="route.cloud.claude",
    ))

    assert result.demoted is True
    assert result.detail["from_level"] == 2
    assert result.detail["to_level"] == 1
    assert state.get_effective_wal("route.cloud.claude") == 1


# ===========================================================================
# Capabilities Status Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_capabilities_returns_all(setup):
    """GET /admin/capabilities returns all 12 capabilities."""
    _registry, _state, _audit, _engine, _monitor = setup

    result = await get_capabilities()

    assert len(result.capabilities) == 12
    for cap in result.capabilities:
        assert "capability_id" in cap
        assert "effective_wal_level" in cap
        assert "status" in cap
        assert "approval_score" in cap


@pytest.mark.asyncio
async def test_capabilities_reflects_state_changes(setup):
    """Record outcomes then verify capabilities endpoint reflects them."""
    _registry, _state, _audit, _engine, _monitor = setup

    for _ in range(5):
        await record_outcome(RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="accepted",
        ))

    result = await get_capabilities()

    claude_caps = [c for c in result.capabilities if c["capability_id"] == "route.cloud.claude"]
    assert len(claude_caps) == 1
    assert claude_caps[0]["outcomes_count"] == 5
    assert claude_caps[0]["approval_score"] == 1.0


# ===========================================================================
# Integration Scenario Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_full_promotion_cycle(setup):
    """End-to-end: record outcomes, satisfy criteria, verify promotion recommendation."""
    _registry, state, audit, _engine, _monitor = setup

    # Verify starts at WAL-0
    assert state.get_effective_wal("route.cloud.claude") == 0

    # Record 30 accepted outcomes
    for _ in range(30):
        await record_outcome(RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="accepted",
        ))

    # Manually set first_job_date to 8 days ago (to satisfy 7-day span)
    entry = state.get("route.cloud.claude")
    entry["counters"]["first_job_date"] = _days_ago_iso(8)
    state.save()

    # Record one more outcome to trigger the promotion check
    result = await record_outcome(RecordOutcomeRequest(
        capability_id="route.cloud.claude",
        disposition="accepted",
    ))

    # Verify promotion check returns ready: true
    assert result.promotion_check["ready"] is True
    assert result.promotion_check["recommended_level"] == 1

    # Verify system.notify event was emitted
    notify_events = [e for e in audit.events if e["event_type"] == "system.notify"]
    assert len(notify_events) >= 1
    latest_notify = notify_events[-1]
    assert latest_notify["payload"]["notification_type"] == "promotion_recommendation"

    # Verify capabilities endpoint shows updated state
    caps_result = await get_capabilities()
    claude_caps = [c for c in caps_result.capabilities if c["capability_id"] == "route.cloud.claude"]
    assert claude_caps[0]["outcomes_count"] == 31
    assert claude_caps[0]["approval_score"] == 1.0


@pytest.mark.asyncio
async def test_demotion_then_re_earn(setup):
    """Demotion resets counters; new outcomes start fresh."""
    _registry, state, _audit, _engine, _monitor = setup

    # Set to WAL-1
    state.set_effective_wal("route.cloud.claude", 1)

    # Record some outcomes at WAL-1
    for _ in range(5):
        await record_outcome(RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="accepted",
        ))

    assert len(state.get_outcomes("route.cloud.claude")) == 5

    # Trigger model change -> demotion to WAL-0
    result = await trigger_model_change(ModelChangeRequest(
        affected_capabilities=["route.cloud.claude"],
    ))

    assert state.get_effective_wal("route.cloud.claude") == 0
    assert len(result.demotions) == 1

    # Verify counters are reset
    assert len(state.get_outcomes("route.cloud.claude")) == 0

    # Record new outcomes -> verify counter starts fresh
    for _ in range(3):
        await record_outcome(RecordOutcomeRequest(
            capability_id="route.cloud.claude",
            disposition="accepted",
        ))

    assert len(state.get_outcomes("route.cloud.claude")) == 3
