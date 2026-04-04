"""Demotion engine: evaluates demotion triggers and executes WAL transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from audit_client import AuditLogClient
from capability_registry import CapabilityRegistry
from capability_state import FAILURE_DEMOTION_THRESHOLD, CapabilityStateManager
from events import event_wal_demoted

DISPOSITION_SCORES = {
    "accepted": 1.0,
    "modified": 0.5,
    "rejected": 0.0,
    "resubmitted": 0.0,
    "auto_delivered": 1.0,
}


@dataclass
class DemotionResult:
    """Result of a demotion evaluation."""

    demoted: bool  # Whether a demotion actually occurred
    capability_id: str = ""
    from_level: int = 0
    to_level: int = 0
    trigger: str = ""
    reason: str = ""
    source_event_id: str = ""  # From the wal.demoted event, if emitted


class DemotionEngine:
    def __init__(
        self,
        registry: CapabilityRegistry,
        state_manager: CapabilityStateManager,
        audit_client: AuditLogClient,
    ):
        self._registry = registry
        self._state = state_manager
        self._audit = audit_client

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    async def _execute_demotion(
        self,
        capability_id: str,
        to_level: int,
        trigger: str,
        source_event_id: str | None,
    ) -> DemotionResult:
        """Demote, reset counters, set incident ref, emit audit event, return result."""
        from_level = self._state.get_effective_wal(capability_id)
        cap_config = self._registry.get(capability_id)
        capability_name = cap_config["capability_name"] if cap_config else capability_id

        self._state.demote(capability_id, to_level)
        self._state.reset_counters(capability_id)

        if source_event_id:
            entry = self._state.get(capability_id)
            entry["counters"]["last_incident_source_event_id"] = source_event_id
            self._state.save()

        event = event_wal_demoted(
            capability_id=capability_id,
            capability_name=capability_name,
            from_level=from_level,
            to_level=to_level,
            trigger=trigger,
            incident_ref=source_event_id,
        )
        await self._audit.emit_durable(event)

        return DemotionResult(
            demoted=True,
            capability_id=capability_id,
            from_level=from_level,
            to_level=to_level,
            trigger=trigger,
            reason="",
            source_event_id=event["source_event_id"],
        )

    # ------------------------------------------------------------------
    # Method 1: Strip failure
    # ------------------------------------------------------------------

    async def handle_strip_failure(
        self,
        source_event_id: str,
        failing_capability_id: str | None = None,
    ) -> DemotionResult:
        """Handle a strip failure detected by the egress gateway.

        Triggered when: context.strip_detail has detected_by: egress_gateway
        (meaning the packager MISSED something the gateway caught).

        Action: SUSPEND context.package to WAL -1.

        Per Trust Profile S10.1: strip failure -> SUSPEND context.package.
        The failing_capability_id in the strip_detail event identifies who failed,
        but the action is always to suspend context.package specifically.

        Returns DemotionResult indicating whether suspension occurred.
        """
        cap_id = "context.package"

        cap_config = self._registry.get(cap_id)
        if cap_config is None:
            return DemotionResult(demoted=False, reason="context.package not in registry")

        current_wal = self._state.get_effective_wal(cap_id)
        if current_wal == -1:
            return DemotionResult(demoted=False, reason="already_suspended")

        return await self._execute_demotion(cap_id, -1, "strip_failure", source_event_id)

    # ------------------------------------------------------------------
    # Method 2: Job failure (3-in-24h)
    # ------------------------------------------------------------------

    async def handle_job_failure(
        self,
        failing_capability_id: str,
        source_event_id: str,
        timestamp: str,
    ) -> DemotionResult:
        """Handle a job failure and evaluate whether a demotion is triggered.

        Triggered when: job.failed event with a failing_capability_id.

        Rules (Trust Profile S10.1, S10.4):
        1. If failing_capability_id is a sentinel -> NO demotion. Return immediately.
        2. If failing_capability_id is not a registered capability -> NO demotion.
        3. Record the failure in the capability's recent_failures deque.
        4. Evict failures older than 24h.
        5. If failure count >= 3 in rolling 24h -> demote by 1.

        Sentinels excluded from counters:
            "egress_config", "egress_connectivity", "worker_sandbox"

        Returns DemotionResult indicating whether demotion occurred.
        """
        if self._registry.is_sentinel(failing_capability_id):
            return DemotionResult(demoted=False, reason="sentinel_excluded")

        if self._registry.get(failing_capability_id) is None:
            return DemotionResult(demoted=False, reason="unregistered_capability")

        self._state.record_failure(failing_capability_id, timestamp, source_event_id)
        count = self._state.get_recent_failure_count(failing_capability_id)

        if count < FAILURE_DEMOTION_THRESHOLD:
            return DemotionResult(demoted=False, reason="below_threshold")

        current_wal = self._state.get_effective_wal(failing_capability_id)
        if current_wal <= 0:
            return DemotionResult(demoted=False, reason="already_at_floor")

        to_level = current_wal - 1
        return await self._execute_demotion(
            failing_capability_id, to_level, "error", source_event_id
        )

    # ------------------------------------------------------------------
    # Method 3: Override demotion
    # ------------------------------------------------------------------

    async def handle_override_demotion(
        self,
        override_type: str,  # "cancel" or "redirect"
        governing_capability_id: str,
        job_context: dict,  # Must contain at minimum: prior_failure_capability_id
        source_event_id: str,
    ) -> DemotionResult:
        """Handle demotion triggered by a cancel/redirect override on a WAL-1+ job.

        Triggered when: human.override with type cancel or redirect on a job
        whose governing capability is at WAL-1 or above.

        Rules (Trust Profile S10.1, S10.3):
        1. Only cancel and redirect overrides trigger demotion evaluation.
        2. Only if governing capability is at WAL-1 or above.
        3. CONDITIONAL: Check if the job had a prior sentinel failure.
           If the job's most recent job.failed event had failing_capability_id
           set to a sentinel value -> NO demotion (the failure was infrastructure,
           not a routing judgment failure).
        4. If no prior sentinel -> demote governing capability by 1.

        Args:
            override_type: "cancel" or "redirect"
            governing_capability_id: The governing capability of the overridden job
            job_context: Dict with at minimum:
                - "prior_failure_capability_id": str|None -- from the job's last job.failed
            source_event_id: The source_event_id of the human.override event

        Returns DemotionResult
        """
        if override_type not in ("cancel", "redirect"):
            return DemotionResult(demoted=False, reason="not_demotion_override")

        current_wal = self._state.get_effective_wal(governing_capability_id)
        if current_wal < 1:
            return DemotionResult(demoted=False, reason="wal_below_threshold")

        prior_failure_cap = job_context.get("prior_failure_capability_id")
        if prior_failure_cap and self._registry.is_sentinel(prior_failure_cap):
            return DemotionResult(demoted=False, reason="sentinel_failure_exemption")

        to_level = current_wal - 1
        return await self._execute_demotion(
            governing_capability_id, to_level, "override", source_event_id
        )

    # ------------------------------------------------------------------
    # Method 4: Model change
    # ------------------------------------------------------------------

    async def handle_model_change(
        self,
        affected_capabilities: list[str],
        source_event_id: str | None = None,
    ) -> list[DemotionResult]:
        """Handle a model change: demote all affected capabilities to WAL-0.

        Triggered when: system.model_change event (e.g., Ollama model updated).

        Rules (Trust Profile S10.1):
        - Model change -> WAL-0 for all affected capabilities.
        - Each capability gets its own wal.demoted event.
        - Counter reset on each.

        Args:
            affected_capabilities: List of capability_ids affected by the model change
            source_event_id: The source_event_id of the system.model_change event

        Returns list of DemotionResult, one per affected capability
        """
        results: list[DemotionResult] = []

        for cap_id in affected_capabilities:
            current_wal = self._state.get_effective_wal(cap_id)
            if current_wal == 0:
                continue

            result = await self._execute_demotion(cap_id, 0, "model_change", source_event_id)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Method 5: Approval decay
    # ------------------------------------------------------------------

    async def handle_approval_decay(
        self,
        capability_id: str,
    ) -> DemotionResult:
        """Check WAL-3 approval decay: if approval < 99% over 90d, demote to WAL-2.

        Triggered: periodically (cron) or after each outcome recording.

        Rules (Trust Profile S10.1):
        - Only applies to WAL-3 capabilities.
        - Check evaluable_outcomes for approval score.
        - If score < 0.99 -> demote to WAL-2.

        Returns DemotionResult
        """
        current_wal = self._state.get_effective_wal(capability_id)
        if current_wal != 3:
            return DemotionResult(demoted=False, reason="not_wal_3")

        outcomes = self._state.get_outcomes(capability_id)
        if not outcomes:
            return DemotionResult(demoted=False, reason="no_outcomes")

        score = sum(
            DISPOSITION_SCORES.get(o["disposition"], 0.0) for o in outcomes
        ) / len(outcomes)

        if score >= 0.99:
            return DemotionResult(demoted=False, reason="score_acceptable")

        return await self._execute_demotion(capability_id, 2, "anomaly", None)
