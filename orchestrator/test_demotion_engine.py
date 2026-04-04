"""Tests for demotion_engine.py (Phase 2C)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from demotion_engine import DemotionEngine, DemotionResult
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="microseconds"
    )


@pytest.fixture
def setup(tmp_path):
    """Provide registry, state manager, audit client, and demotion engine."""
    registry = _make_registry()
    state = _make_state(registry, str(tmp_path))
    audit = MockAuditClient()
    engine = DemotionEngine(registry, state, audit)
    return registry, state, audit, engine


# ===========================================================================
# Strip Failure Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_strip_failure_suspends_context_package(setup):
    """handle_strip_failure suspends context.package to WAL -1."""
    registry, state, audit, engine = setup

    result = await engine.handle_strip_failure(source_event_id="evt-123")

    assert result.demoted is True
    assert result.capability_id == "context.package"
    assert result.to_level == -1
    assert result.trigger == "strip_failure"

    # Verify state
    assert state.get_effective_wal("context.package") == -1
    assert state.get_status("context.package") == "suspended"

    # Verify audit event
    assert len(audit.events) == 1
    evt = audit.events[0]
    assert evt["event_type"] == "wal.demoted"
    assert evt["payload"]["trigger"] == "strip_failure"
    assert evt["payload"]["to_level"] == -1
    assert evt["payload"]["incident_ref"] == "evt-123"

    # Verify counters were reset
    entry = state.get("context.package")
    assert entry["counters"]["evaluable_outcomes"] == []
    assert entry["counters"]["recent_failures"] == []
    assert entry["counters"]["first_job_date"] is None


@pytest.mark.asyncio
async def test_strip_failure_already_suspended(setup):
    """If context.package is already suspended, no demotion or event."""
    _registry, state, audit, engine = setup

    state.suspend("context.package")
    audit.events.clear()

    result = await engine.handle_strip_failure(source_event_id="evt-456")

    assert result.demoted is False
    assert result.reason == "already_suspended"
    assert len(audit.events) == 0


@pytest.mark.asyncio
async def test_strip_failure_preserves_incident_ref(setup):
    """After strip failure, last_incident_source_event_id is set."""
    _registry, state, _audit, engine = setup

    await engine.handle_strip_failure(source_event_id="evt-789")

    entry = state.get("context.package")
    assert entry["counters"]["last_incident_source_event_id"] == "evt-789"


# ===========================================================================
# Job Failure / 3-in-24h Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_three_failures_demotes(setup):
    """3 failures within 24h demotes route.cloud.claude from WAL-1 to WAL-0."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)
    now = _now_iso()

    for i in range(3):
        result = await engine.handle_job_failure(
            failing_capability_id="route.cloud.claude",
            source_event_id=f"evt-fail-{i}",
            timestamp=now,
        )

    assert result.demoted is True
    assert result.from_level == 1
    assert result.to_level == 0
    assert result.trigger == "error"
    assert state.get_effective_wal("route.cloud.claude") == 0

    # Verify wal.demoted event
    demoted_events = [e for e in audit.events if e["event_type"] == "wal.demoted"]
    assert len(demoted_events) == 1
    assert demoted_events[0]["payload"]["trigger"] == "error"

    # Verify counter reset
    entry = state.get("route.cloud.claude")
    assert entry["counters"]["evaluable_outcomes"] == []
    assert entry["counters"]["recent_failures"] == []


@pytest.mark.asyncio
async def test_two_failures_no_demotion(setup):
    """2 failures should not trigger demotion."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)
    now = _now_iso()

    for i in range(2):
        result = await engine.handle_job_failure(
            failing_capability_id="route.cloud.claude",
            source_event_id=f"evt-fail-{i}",
            timestamp=now,
        )

    assert result.demoted is False
    assert result.reason == "below_threshold"
    assert state.get_effective_wal("route.cloud.claude") == 1


@pytest.mark.asyncio
async def test_sentinel_excluded(setup):
    """Sentinel capability egress_connectivity is excluded from demotion."""
    _registry, _state, _audit, engine = setup

    for i in range(3):
        result = await engine.handle_job_failure(
            failing_capability_id="egress_connectivity",
            source_event_id=f"evt-sentinel-{i}",
            timestamp=_now_iso(),
        )

    assert result.demoted is False
    assert result.reason == "sentinel_excluded"


@pytest.mark.asyncio
async def test_sentinel_egress_config_excluded(setup):
    """Sentinel capability egress_config is excluded from demotion."""
    _registry, _state, _audit, engine = setup

    for i in range(3):
        result = await engine.handle_job_failure(
            failing_capability_id="egress_config",
            source_event_id=f"evt-sentinel-{i}",
            timestamp=_now_iso(),
        )

    assert result.demoted is False
    assert result.reason == "sentinel_excluded"


@pytest.mark.asyncio
async def test_sentinel_worker_sandbox_excluded(setup):
    """Sentinel capability worker_sandbox is excluded from demotion."""
    _registry, _state, _audit, engine = setup

    for i in range(3):
        result = await engine.handle_job_failure(
            failing_capability_id="worker_sandbox",
            source_event_id=f"evt-sentinel-{i}",
            timestamp=_now_iso(),
        )

    assert result.demoted is False
    assert result.reason == "sentinel_excluded"


@pytest.mark.asyncio
async def test_failure_at_wal0_no_further_demotion(setup):
    """Capability already at WAL-0 cannot be demoted further by failures."""
    _registry, state, audit, engine = setup

    # route.cloud.claude starts at WAL-0 by default
    assert state.get_effective_wal("route.cloud.claude") == 0
    now = _now_iso()

    for i in range(3):
        result = await engine.handle_job_failure(
            failing_capability_id="route.cloud.claude",
            source_event_id=f"evt-floor-{i}",
            timestamp=now,
        )

    assert result.demoted is False
    assert result.reason == "already_at_floor"
    assert len([e for e in audit.events if e["event_type"] == "wal.demoted"]) == 0


@pytest.mark.asyncio
async def test_counter_reset_on_demotion(setup):
    """After demotion, counters are reset but last_incident_source_event_id is preserved."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)
    now = _now_iso()

    for i in range(3):
        await engine.handle_job_failure(
            failing_capability_id="route.cloud.claude",
            source_event_id=f"evt-reset-{i}",
            timestamp=now,
        )

    entry = state.get("route.cloud.claude")
    counters = entry["counters"]
    assert counters["evaluable_outcomes"] == []
    assert counters["recent_failures"] == []
    assert counters["first_job_date"] is None
    assert counters["last_reset"] is not None
    # Incident ref is preserved (set to the triggering event)
    assert counters["last_incident_source_event_id"] == "evt-reset-2"


@pytest.mark.asyncio
async def test_failure_eviction_clears_old(setup):
    """Failures older than 24h are evicted; only recent ones count."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)
    old_ts = _hours_ago_iso(25)

    # Record 2 old failures
    for i in range(2):
        await engine.handle_job_failure(
            failing_capability_id="route.cloud.claude",
            source_event_id=f"evt-old-{i}",
            timestamp=old_ts,
        )

    # Record 1 recent failure
    result = await engine.handle_job_failure(
        failing_capability_id="route.cloud.claude",
        source_event_id="evt-recent-0",
        timestamp=_now_iso(),
    )

    assert result.demoted is False
    assert result.reason == "below_threshold"
    # Only 1 recent failure after eviction
    assert state.get_recent_failure_count("route.cloud.claude") == 1
    assert len([e for e in audit.events if e["event_type"] == "wal.demoted"]) == 0


@pytest.mark.asyncio
async def test_unregistered_capability_no_demotion(setup):
    """Unregistered capability ID results in no demotion."""
    _registry, _state, _audit, engine = setup

    result = await engine.handle_job_failure(
        failing_capability_id="nonexistent.cap",
        source_event_id="evt-unreg-0",
        timestamp=_now_iso(),
    )

    assert result.demoted is False
    assert result.reason == "unregistered_capability"


# ===========================================================================
# Override Demotion Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_override_demotes_wal1(setup):
    """Cancel override on WAL-1 capability demotes to WAL-0."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)

    result = await engine.handle_override_demotion(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
        job_context={"prior_failure_capability_id": None},
        source_event_id="evt-override-1",
    )

    assert result.demoted is True
    assert result.from_level == 1
    assert result.to_level == 0
    assert state.get_effective_wal("route.cloud.claude") == 0

    demoted_events = [e for e in audit.events if e["event_type"] == "wal.demoted"]
    assert len(demoted_events) == 1
    assert demoted_events[0]["payload"]["trigger"] == "override"


@pytest.mark.asyncio
async def test_redirect_override_demotes_wal2(setup):
    """Redirect override on WAL-2 capability demotes to WAL-1."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 2)

    result = await engine.handle_override_demotion(
        override_type="redirect",
        governing_capability_id="route.cloud.claude",
        job_context={"prior_failure_capability_id": None},
        source_event_id="evt-override-2",
    )

    assert result.demoted is True
    assert result.from_level == 2
    assert result.to_level == 1
    assert state.get_effective_wal("route.cloud.claude") == 1


@pytest.mark.asyncio
async def test_override_at_wal0_no_demotion(setup):
    """Override on WAL-0 capability does not demote."""
    _registry, state, _audit, engine = setup

    assert state.get_effective_wal("route.cloud.claude") == 0

    result = await engine.handle_override_demotion(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
        job_context={"prior_failure_capability_id": None},
        source_event_id="evt-override-3",
    )

    assert result.demoted is False
    assert result.reason == "wal_below_threshold"


@pytest.mark.asyncio
async def test_override_with_sentinel_failure_no_demotion(setup):
    """Override with prior sentinel failure exempts from demotion."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)

    result = await engine.handle_override_demotion(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
        job_context={"prior_failure_capability_id": "egress_connectivity"},
        source_event_id="evt-override-4",
    )

    assert result.demoted is False
    assert result.reason == "sentinel_failure_exemption"


@pytest.mark.asyncio
async def test_modify_override_no_demotion(setup):
    """Modify override type does not trigger demotion."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 2)

    result = await engine.handle_override_demotion(
        override_type="modify",
        governing_capability_id="route.cloud.claude",
        job_context={"prior_failure_capability_id": None},
        source_event_id="evt-override-5",
    )

    assert result.demoted is False
    assert result.reason == "not_demotion_override"


# ===========================================================================
# Model Change Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_model_change_demotes_all(setup):
    """Model change demotes all affected capabilities to WAL-0."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 2)
    state.set_effective_wal("route.cloud.openai", 1)

    results = await engine.handle_model_change(
        affected_capabilities=["route.cloud.claude", "route.cloud.openai"],
        source_event_id="evt-model-1",
    )

    assert len(results) == 2
    assert all(r.demoted for r in results)
    assert state.get_effective_wal("route.cloud.claude") == 0
    assert state.get_effective_wal("route.cloud.openai") == 0

    demoted_events = [e for e in audit.events if e["event_type"] == "wal.demoted"]
    assert len(demoted_events) == 2
    assert all(e["payload"]["trigger"] == "model_change" for e in demoted_events)


@pytest.mark.asyncio
async def test_model_change_already_wal0(setup):
    """Capability already at WAL-0 is skipped during model change."""
    _registry, state, audit, engine = setup

    assert state.get_effective_wal("route.cloud.claude") == 0

    results = await engine.handle_model_change(
        affected_capabilities=["route.cloud.claude"],
        source_event_id="evt-model-2",
    )

    assert len(results) == 0
    assert len([e for e in audit.events if e["event_type"] == "wal.demoted"]) == 0


@pytest.mark.asyncio
async def test_model_change_counter_reset(setup):
    """After model change demotion, counters are reset for each affected capability."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 2)
    # Add some outcomes and failures to verify they get cleared
    state.record_outcome("route.cloud.claude", {"disposition": "accepted"})
    state.record_failure("route.cloud.claude", _now_iso(), "evt-pre-fail")

    await engine.handle_model_change(
        affected_capabilities=["route.cloud.claude"],
        source_event_id="evt-model-3",
    )

    entry = state.get("route.cloud.claude")
    counters = entry["counters"]
    assert counters["evaluable_outcomes"] == []
    assert counters["recent_failures"] == []
    assert counters["first_job_date"] is None
    assert counters["last_reset"] is not None


# ===========================================================================
# Approval Decay Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_approval_decay_wal3_below_threshold(setup):
    """WAL-3 capability with <99% approval is demoted to WAL-2."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("notify.watch", 3)

    # Record 95 accepted, 5 rejected -> score = 0.95
    for _ in range(95):
        state.record_outcome("notify.watch", {"disposition": "accepted"})
    for _ in range(5):
        state.record_outcome("notify.watch", {"disposition": "rejected"})

    result = await engine.handle_approval_decay("notify.watch")

    assert result.demoted is True
    assert result.from_level == 3
    assert result.to_level == 2
    assert result.trigger == "anomaly"
    assert state.get_effective_wal("notify.watch") == 2

    demoted_events = [e for e in audit.events if e["event_type"] == "wal.demoted"]
    assert len(demoted_events) == 1
    assert demoted_events[0]["payload"]["trigger"] == "anomaly"


@pytest.mark.asyncio
async def test_approval_decay_wal3_above_threshold(setup):
    """WAL-3 capability with >=99% approval is not demoted."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("notify.watch", 3)

    # 99 accepted + 1 modified -> score = (99*1.0 + 1*0.5)/100 = 0.995
    for _ in range(99):
        state.record_outcome("notify.watch", {"disposition": "accepted"})
    state.record_outcome("notify.watch", {"disposition": "modified"})

    result = await engine.handle_approval_decay("notify.watch")

    assert result.demoted is False
    assert result.reason == "score_acceptable"


@pytest.mark.asyncio
async def test_approval_decay_not_wal3(setup):
    """Approval decay only applies to WAL-3 capabilities."""
    _registry, state, _audit, engine = setup

    state.set_effective_wal("notify.watch", 2)

    result = await engine.handle_approval_decay("notify.watch")

    assert result.demoted is False
    assert result.reason == "not_wal_3"


# ===========================================================================
# Event Verification Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_demoted_event_payload_complete(setup):
    """wal.demoted audit event has all required payload fields."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)
    now = _now_iso()

    for i in range(3):
        await engine.handle_job_failure(
            failing_capability_id="route.cloud.claude",
            source_event_id=f"evt-payload-{i}",
            timestamp=now,
        )

    demoted_events = [e for e in audit.events if e["event_type"] == "wal.demoted"]
    assert len(demoted_events) == 1
    payload = demoted_events[0]["payload"]

    assert "capability_id" in payload
    assert "capability_name" in payload
    assert "from_level" in payload
    assert "to_level" in payload
    assert "trigger" in payload
    assert "incident_ref" in payload

    assert payload["capability_id"] == "route.cloud.claude"
    assert payload["capability_name"] == "Claude Cloud Dispatch"
    assert payload["from_level"] == 1
    assert payload["to_level"] == 0
    assert payload["trigger"] == "error"
    assert payload["incident_ref"] == "evt-payload-2"


@pytest.mark.asyncio
async def test_demoted_event_source_event_id(setup):
    """DemotionResult.source_event_id matches the emitted wal.demoted event."""
    _registry, state, audit, engine = setup

    state.set_effective_wal("route.cloud.claude", 1)

    result = await engine.handle_override_demotion(
        override_type="cancel",
        governing_capability_id="route.cloud.claude",
        job_context={"prior_failure_capability_id": None},
        source_event_id="evt-verify-1",
    )

    assert result.demoted is True
    assert result.source_event_id != ""

    demoted_events = [e for e in audit.events if e["event_type"] == "wal.demoted"]
    assert len(demoted_events) == 1
    assert result.source_event_id == demoted_events[0]["source_event_id"]
