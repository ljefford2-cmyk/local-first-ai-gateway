"""Tests for promotion_monitor.py (Phase 2E)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from promotion_monitor import PromotionMonitor, PromotionCheckResult, PromotionEvidence
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


def _days_ago_iso(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="microseconds"
    )


def _feed_outcomes(state_manager, capability_id, count, disposition="accepted", cost=0.01):
    """Record `count` outcomes with the given disposition."""
    for i in range(count):
        state_manager.record_outcome(capability_id, {
            "source_event_id": f"evt-{i:04d}",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "disposition": disposition,
            "cost_usd": cost,
        })


@pytest.fixture
def setup(tmp_path):
    """Provide registry, state manager, audit client, and promotion monitor."""
    registry = _make_registry()
    state = _make_state(registry, str(tmp_path))
    audit = MockAuditClient()
    monitor = PromotionMonitor(registry, state, audit)
    return registry, state, audit, monitor


# ===========================================================================
# Score Calculation Tests
# ===========================================================================


class TestApprovalScore:
    def test_approval_score_all_accepted(self, setup):
        """Feed 30 outcomes all accepted. Score should be 1.0."""
        _registry, state, _audit, _monitor = setup

        _feed_outcomes(state, "route.cloud.claude", 30, disposition="accepted")
        score = state.compute_approval_score("route.cloud.claude")

        assert score == 1.0

    def test_approval_score_mixed(self, setup):
        """Feed 28 accepted + 2 modified. Score = (28*1.0 + 2*0.5) / 30 = 0.9667."""
        _registry, state, _audit, _monitor = setup

        _feed_outcomes(state, "route.cloud.claude", 28, disposition="accepted")
        _feed_outcomes(state, "route.cloud.claude", 2, disposition="modified")
        score = state.compute_approval_score("route.cloud.claude")

        expected = (28 * 1.0 + 2 * 0.5) / 30
        assert abs(score - expected) < 1e-4

    def test_approval_score_with_rejections(self, setup):
        """Feed 25 accepted + 5 rejected. Score = 25/30 = 0.8333."""
        _registry, state, _audit, _monitor = setup

        _feed_outcomes(state, "route.cloud.claude", 25, disposition="accepted")
        _feed_outcomes(state, "route.cloud.claude", 5, disposition="rejected")
        score = state.compute_approval_score("route.cloud.claude")

        expected = 25.0 / 30
        assert abs(score - expected) < 1e-4

    def test_approval_score_empty(self, setup):
        """No outcomes. Score should be None."""
        _registry, state, _audit, _monitor = setup

        score = state.compute_approval_score("route.cloud.claude")

        assert score is None


# ===========================================================================
# Criteria Not Met Tests
# ===========================================================================


class TestCriteriaNotMet:
    @pytest.mark.asyncio
    async def test_insufficient_outcomes(self, setup):
        """Feed 20 outcomes (need 30) over 10 days, all accepted. Not ready."""
        _registry, state, _audit, monitor = setup

        state.set_first_job_date("route.cloud.claude", _days_ago_iso(10))
        _feed_outcomes(state, "route.cloud.claude", 20, disposition="accepted")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is False
        assert result.reason == "insufficient_outcomes"
        assert result.evidence.evaluable_outcomes == 20

    @pytest.mark.asyncio
    async def test_insufficient_span(self, setup):
        """Feed 30 outcomes all accepted. first_job_date 5 days ago (need 7). Not ready."""
        _registry, state, _audit, monitor = setup

        state.set_first_job_date("route.cloud.claude", _days_ago_iso(5))
        _feed_outcomes(state, "route.cloud.claude", 30, disposition="accepted")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is False
        assert result.reason == "insufficient_span"

    @pytest.mark.asyncio
    async def test_score_below_threshold(self, setup):
        """Feed 30 outcomes over 10 days. Score ~0.90 (mix). Not ready."""
        _registry, state, _audit, monitor = setup

        state.set_first_job_date("route.cloud.claude", _days_ago_iso(10))
        # 27 accepted + 3 rejected = score 27/30 = 0.90
        _feed_outcomes(state, "route.cloud.claude", 27, disposition="accepted")
        _feed_outcomes(state, "route.cloud.claude", 3, disposition="rejected")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is False
        assert result.reason == "score_below_threshold"
        assert result.evidence.approval_score < 0.95

    @pytest.mark.asyncio
    async def test_no_first_job_date(self, setup):
        """Feed 30 outcomes but leave first_job_date as None. Not ready."""
        _registry, state, _audit, monitor = setup

        _feed_outcomes(state, "route.cloud.claude", 30, disposition="accepted")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is False
        assert result.reason == "no_first_job_date"


# ===========================================================================
# Criteria Met - Promotion Recommended Tests
# ===========================================================================


class TestPromotionRecommended:
    @pytest.mark.asyncio
    async def test_promotion_0_to_1_recommended(self, setup):
        """Feed 30 outcomes over 10 days, score 0.97, no incidents. Promotion recommended."""
        _registry, state, audit, monitor = setup

        state.set_first_job_date("route.cloud.claude", _days_ago_iso(10))
        # 29 accepted + 1 modified = score (29*1.0 + 1*0.5)/30 = 0.9833
        _feed_outcomes(state, "route.cloud.claude", 29, disposition="accepted")
        _feed_outcomes(state, "route.cloud.claude", 1, disposition="modified")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is True
        assert result.reason == "criteria_met"
        assert result.capability_id == "route.cloud.claude"
        assert result.current_level == 0
        assert result.recommended_level == 1
        assert result.notification_emitted is True
        assert result.source_event_id != ""

        # Verify evidence
        assert result.evidence is not None
        assert result.evidence.evaluable_outcomes == 30
        assert result.evidence.approval_score >= 0.95

        # Verify system.notify event emitted
        notify_events = [e for e in audit.events if e["event_type"] == "system.notify"]
        assert len(notify_events) == 1
        evt = notify_events[0]
        assert evt["payload"]["notification_type"] == "promotion_recommendation"
        assert "route.cloud.claude" in evt["payload"]["title"]
        assert evt["payload"]["detail"]["type"] == "promotion_recommendation"

    @pytest.mark.asyncio
    async def test_promotion_1_to_2_recommended(self, setup):
        """Set route.cloud.claude to WAL-1. Feed 100 outcomes over 35 days, score 0.99. Recommended."""
        _registry, state, audit, monitor = setup

        state.set_effective_wal("route.cloud.claude", 1)
        state.set_first_job_date("route.cloud.claude", _days_ago_iso(35))
        # 99 accepted + 1 modified = score (99 + 0.5)/100 = 0.995
        _feed_outcomes(state, "route.cloud.claude", 99, disposition="accepted")
        _feed_outcomes(state, "route.cloud.claude", 1, disposition="modified")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is True
        assert result.reason == "criteria_met"
        assert result.current_level == 1
        assert result.recommended_level == 2
        assert result.notification_emitted is True


# ===========================================================================
# Edge Cases Tests
# ===========================================================================


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_suspended_capability(self, setup):
        """Suspended capability returns not ready with reason 'suspended'."""
        _registry, state, _audit, monitor = setup

        state.suspend("route.cloud.claude")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is False
        assert result.reason == "suspended"
        assert result.current_level == -1

    @pytest.mark.asyncio
    async def test_promotion_not_available(self, setup):
        """Check promotion for a capability at WAL-2 where 2_to_3 is null but max_wal allows it."""
        _registry, state, _audit, monitor = setup

        # notify.watch has max_wal: 3 but 2_to_3: null, so promotion is not available
        state.set_effective_wal("notify.watch", 2)

        result = await monitor.check_promotion("notify.watch")

        assert result.ready is False
        assert result.reason == "promotion_not_available"

    @pytest.mark.asyncio
    async def test_exceeds_max_wal(self, setup):
        """Set a governing capability to max_wal. Next level exceeds max_wal."""
        _registry, state, _audit, monitor = setup

        # route.cloud.claude has max_wal: 2. Set it to WAL-2.
        # next_level = 3, but 2_to_3 criteria is null so "promotion_not_available"
        # hits first. Instead, let's use a capability where max_wal could be tested.
        # Actually, the spec says check exceeds_max_wal before criteria lookup.
        # But in our implementation, we check max_wal first then criteria.
        # For max_wal=2, next_level=3: 3 > 2 -> exceeds_max_wal.
        state.set_effective_wal("route.cloud.claude", 2)

        result = await monitor.check_promotion("route.cloud.claude")

        # max_wal is 2 for route.cloud.claude, next_level=3 > 2
        assert result.ready is False
        assert result.reason == "exceeds_max_wal"

    @pytest.mark.asyncio
    async def test_incident_blocks_1_to_2(self, setup):
        """WAL-1 with incident ref set blocks promotion to WAL-2."""
        _registry, state, _audit, monitor = setup

        state.set_effective_wal("route.cloud.claude", 1)
        state.set_first_job_date("route.cloud.claude", _days_ago_iso(35))
        _feed_outcomes(state, "route.cloud.claude", 100, disposition="accepted")

        # Set incident ref
        entry = state.get("route.cloud.claude")
        entry["counters"]["last_incident_source_event_id"] = "evt-incident-001"
        state.save()

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is False
        assert result.reason == "incident_exists"

    @pytest.mark.asyncio
    async def test_ring_buffer_full(self, setup):
        """Feed 250 outcomes. Only 200 are in the buffer. Score computed on 200."""
        _registry, state, _audit, monitor = setup

        _feed_outcomes(state, "route.cloud.claude", 250, disposition="accepted")

        outcomes = state.get_outcomes("route.cloud.claude")
        assert len(outcomes) == 200

        score = state.compute_approval_score("route.cloud.claude")
        assert score == 1.0


# ===========================================================================
# Notification Event Tests
# ===========================================================================


class TestNotificationEvent:
    @pytest.mark.asyncio
    async def test_notification_event_payload(self, setup):
        """Verify system.notify event structure after promotion recommendation."""
        _registry, state, audit, monitor = setup

        state.set_first_job_date("route.cloud.claude", _days_ago_iso(10))
        _feed_outcomes(state, "route.cloud.claude", 30, disposition="accepted")

        await monitor.check_promotion("route.cloud.claude")

        notify_events = [e for e in audit.events if e["event_type"] == "system.notify"]
        assert len(notify_events) == 1

        evt = notify_events[0]
        payload = evt["payload"]

        assert payload["notification_type"] == "promotion_recommendation"
        assert "route.cloud.claude" in payload["title"]
        assert "WAL-0" in payload["title"]
        assert "WAL-1" in payload["title"]

        detail = payload["detail"]
        assert detail["capability_id"] == "route.cloud.claude"
        assert detail["current_level"] == 0
        assert detail["recommended_level"] == 1
        assert "evidence" in detail
        assert detail["evidence"]["evaluable_outcomes"] == 30
        assert detail["evidence"]["approval_score"] == 1.0

    @pytest.mark.asyncio
    async def test_notification_source_event_id(self, setup):
        """PromotionCheckResult.source_event_id matches the emitted event."""
        _registry, state, audit, monitor = setup

        state.set_first_job_date("route.cloud.claude", _days_ago_iso(10))
        _feed_outcomes(state, "route.cloud.claude", 30, disposition="accepted")

        result = await monitor.check_promotion("route.cloud.claude")

        assert result.ready is True
        assert result.source_event_id != ""

        notify_events = [e for e in audit.events if e["event_type"] == "system.notify"]
        assert len(notify_events) == 1
        assert result.source_event_id == notify_events[0]["source_event_id"]


# ===========================================================================
# Auxiliary Capability Promotion Tests
# ===========================================================================


class TestAuxiliaryPromotion:
    @pytest.mark.asyncio
    async def test_context_package_promotion(self, setup):
        """context.package promotion checks zero_egress_catches and passes for active capability."""
        _registry, state, audit, monitor = setup

        state.set_first_job_date("context.package", _days_ago_iso(10))
        _feed_outcomes(state, "context.package", 30, disposition="accepted")

        result = await monitor.check_promotion("context.package")

        assert result.ready is True
        assert result.reason == "criteria_met"
        assert result.current_level == 0
        assert result.recommended_level == 1

        # Verify notification emitted
        notify_events = [e for e in audit.events if e["event_type"] == "system.notify"]
        assert len(notify_events) == 1


# ===========================================================================
# Check All Tests
# ===========================================================================


class TestCheckAll:
    @pytest.mark.asyncio
    async def test_check_all_returns_all(self, setup):
        """check_all returns results for all capabilities (excluding _meta)."""
        _registry, _state, _audit, monitor = setup

        results = await monitor.check_all()

        # Config has 12 capabilities (no _meta key)
        assert len(results) == 12
        cap_ids = {r.capability_id for r in results}
        assert "route.cloud.claude" in cap_ids
        assert "context.package" in cap_ids
        assert "notify.watch" in cap_ids


# ===========================================================================
# First Job Date Tests
# ===========================================================================


class TestFirstJobDate:
    def test_set_first_job_date_only_once(self, setup):
        """set_first_job_date only sets the first time; subsequent calls are no-ops."""
        _registry, state, _audit, _monitor = setup

        first_ts = "2026-03-01T00:00:00.000000+00:00"
        second_ts = "2026-03-15T00:00:00.000000+00:00"

        state.set_first_job_date("route.cloud.claude", first_ts)
        entry = state.get("route.cloud.claude")
        assert entry["counters"]["first_job_date"] == first_ts

        state.set_first_job_date("route.cloud.claude", second_ts)
        entry = state.get("route.cloud.claude")
        assert entry["counters"]["first_job_date"] == first_ts  # Unchanged
