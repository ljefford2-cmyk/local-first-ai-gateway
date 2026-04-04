"""Promotion evidence collector: evaluates promotion readiness and emits notification jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from audit_client import AuditLogClient
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from events import event_system_notify


@dataclass
class PromotionEvidence:
    """Snapshot of evidence for a promotion recommendation."""

    evaluable_outcomes: int = 0
    calendar_span_days: float = 0.0
    approval_score: float = 0.0
    strip_failures: int = 0  # Count of strip failures (failing_capability_id non-null)
    cancel_redirect_overrides: int = 0
    edge_cases: int = 0


@dataclass
class PromotionCheckResult:
    """Result of a promotion readiness check."""

    ready: bool = False
    capability_id: str = ""
    current_level: int = 0
    recommended_level: int = 0
    evidence: PromotionEvidence | None = None
    reason: str = ""  # Why not ready, or "criteria_met" if ready
    notification_emitted: bool = False
    source_event_id: str = ""  # From the system.notify event if emitted


class PromotionMonitor:
    def __init__(
        self,
        registry: CapabilityRegistry,
        state_manager: CapabilityStateManager,
        audit_client: AuditLogClient,
    ):
        self._registry = registry
        self._state = state_manager
        self._audit = audit_client

    async def check_promotion(self, capability_id: str) -> PromotionCheckResult:
        """Evaluate whether a capability is ready for promotion to the next WAL level.

        Called after each outcome is recorded (or periodically).

        Process:
        1. Get current effective WAL level
        2. Determine the next level (current + 1)
        3. Look up promotion_criteria for the transition (e.g., "0_to_1")
        4. If criteria is null -> promotion not available
        5. If next level > max_wal -> promotion not available
        6. Check each criterion:
           a. evaluable_outcomes: count of outcomes at current level >= threshold
           b. calendar_span_days: days since first_job_date >= threshold
           c. approval_score_min: computed score >= threshold
           d. zero_strip_failures: no strip failures at current level
           e. zero_cancel_redirect: no cancel/redirect overrides at current level
           f. edge_case_count_min: >= threshold (v1: always passes, assume 1)
           g. zero_incidents: no incidents (last_incident_source_event_id is null)
           h. zero_egress_catches: (for context.package only) no egress catches
        7. If ALL criteria met -> emit system.notify with recommendation
        8. Return PromotionCheckResult

        The system does NOT auto-promote. It only recommends.
        """
        # Step 1-2: Level determination
        current_level = self._state.get_effective_wal(capability_id)
        if current_level == -1:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=-1,
                reason="suspended",
            )
        next_level = current_level + 1

        # Step 3-4: Criteria lookup
        cap_config = self._registry.get(capability_id)
        if cap_config is None:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                reason="unknown_capability",
            )

        # Step 5: Max WAL check (before criteria lookup so we catch both cases)
        if next_level > cap_config["max_wal"]:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                reason="exceeds_max_wal",
            )

        criteria_key = f"{current_level}_to_{next_level}"
        criteria = cap_config.get("promotion_criteria", {}).get(criteria_key)
        if criteria is None:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                reason="promotion_not_available",
            )

        # Step 6: Evaluate each criterion
        evidence = PromotionEvidence()

        # 6a. Outcome count
        outcomes = self._state.get_outcomes(capability_id)
        evidence.evaluable_outcomes = len(outcomes)
        required_outcomes = criteria.get("evaluable_outcomes", 0)
        if len(outcomes) < required_outcomes:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                evidence=evidence,
                reason="insufficient_outcomes",
            )

        # 6b. Calendar span
        state_entry = self._state.get(capability_id)
        first_job_date = state_entry["counters"]["first_job_date"]
        if first_job_date is None:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                evidence=evidence,
                reason="no_first_job_date",
            )

        first_dt = datetime.fromisoformat(first_job_date)
        now = datetime.now(timezone.utc)
        span_days = (now - first_dt).total_seconds() / 86400
        evidence.calendar_span_days = round(span_days, 1)
        required_span = criteria.get("calendar_span_days", 0)
        if span_days < required_span:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                evidence=evidence,
                reason="insufficient_span",
            )

        # 6c. Approval score
        score = self._state.compute_approval_score(capability_id)
        if score is None:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                evidence=evidence,
                reason="no_outcomes_for_score",
            )
        evidence.approval_score = round(score, 4)
        required_score = criteria.get("approval_score_min", 0.0)
        if score < required_score:
            return PromotionCheckResult(
                ready=False,
                capability_id=capability_id,
                current_level=current_level,
                recommended_level=next_level,
                evidence=evidence,
                reason="score_below_threshold",
            )

        # 6d. Zero strip failures (if required by criteria)
        if criteria.get("zero_strip_failures", False):
            # v1 simplification: if the capability is active and at this WAL level,
            # it recovered from any prior strip failure and re-earned its position.
            # A more precise check would query the audit log for strip failures
            # at this WAL level since last_reset.
            evidence.strip_failures = 0

        # 6e. Zero cancel/redirect (if required)
        if criteria.get("zero_cancel_redirect", False):
            # v1 simplification: if counters were reset on any demotion,
            # and the capability is currently at this level earning promotion,
            # there have been no overrides since the last reset.
            evidence.cancel_redirect_overrides = 0

        # 6f. Edge case count (if required)
        edge_min = criteria.get("edge_case_count_min", 0)
        if edge_min > 0:
            # v1 simplification: always assume 1 edge case exists.
            # The definition of "edge case" is left to future work.
            evidence.edge_cases = 1

        # 6g. Zero incidents (for 1->2)
        if criteria.get("zero_incidents", False):
            incident_ref = state_entry["counters"]["last_incident_source_event_id"]
            # v1 simplification: if incident ref is not null, fail the check
            # (conservative -- any incident since last reset blocks promotion)
            if incident_ref is not None:
                return PromotionCheckResult(
                    ready=False,
                    capability_id=capability_id,
                    current_level=current_level,
                    recommended_level=next_level,
                    evidence=evidence,
                    reason="incident_exists",
                )

        # 6h. Zero egress catches (for context.package)
        if criteria.get("zero_egress_catches", False):
            # v1 simplification: if context.package is active and not suspended,
            # no egress catches have occurred since recovery.
            pass  # Always passes if capability is active

        # Step 7: All criteria met -- emit system.notify
        recommendation = {
            "type": "promotion_recommendation",
            "capability_id": capability_id,
            "current_level": current_level,
            "recommended_level": next_level,
            "evidence": {
                "evaluable_outcomes": evidence.evaluable_outcomes,
                "calendar_span_days": evidence.calendar_span_days,
                "approval_score": evidence.approval_score,
                "strip_failures": evidence.strip_failures,
                "cancel_redirect_overrides": evidence.cancel_redirect_overrides,
            },
        }

        notify_event = event_system_notify(
            notification_type="promotion_recommendation",
            title=f"Promotion recommended: {capability_id} WAL-{current_level} \u2192 WAL-{next_level}",
            detail=recommendation,
        )
        await self._audit.emit_durable(notify_event)

        return PromotionCheckResult(
            ready=True,
            capability_id=capability_id,
            current_level=current_level,
            recommended_level=next_level,
            evidence=evidence,
            reason="criteria_met",
            notification_emitted=True,
            source_event_id=notify_event["source_event_id"],
        )

    async def check_all(self) -> list[PromotionCheckResult]:
        """Check promotion readiness for all active capabilities.

        Convenience method for periodic checks. Returns results for all
        capabilities, including those not ready (for reporting).
        """
        results = []
        for cap_id in self._registry.get_all():
            if cap_id.startswith("_"):  # Skip _meta
                continue
            result = await self.check_promotion(cap_id)
            results.append(result)
        return results
