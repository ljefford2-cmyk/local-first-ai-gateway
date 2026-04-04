"""Permission checker: evaluates action policies from the capability registry.

Replaces the Phase 2 hardcoded WAL stub with a real policy evaluator that
reads from the CapabilityRegistry and CapabilityStateManager, emitting durable
wal.permission_check audit events for every check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from audit_client import AuditLogClient
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from events import event_wal_permission_check

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    """Context passed to the permission checker for a specific job."""
    job_id: str
    est_cost: float | None = None
    human_approved: bool = False
    result_accepted: bool = False


@dataclass
class PermissionResult:
    """Result of a single permission check."""
    result: str  # "allowed" | "blocked" | "held"
    delivery_hold: bool = False
    hold_type: str | None = None  # "pre_action" | "cost_approval" | "on_accept" | None
    block_reason: str | None = None
    dependency_mode: str | None = None  # "required" | "optional" | "best_effort" | None
    capability_id: str = ""
    requested_action: str = ""
    current_level: int = 0
    required_level: int | None = None
    source_event_id: str = ""  # Set after audit event is emitted


def _compute_required_level(cap: dict, action: str) -> int | None:
    """Find the minimum WAL level at which the action becomes non-blocked.

    Scans levels 0-3. Returns None if the action is never permitted.
    """
    for level in range(4):
        level_policies = cap["action_policies"].get(str(level), {})
        if not isinstance(level_policies, dict):
            continue
        policy = level_policies.get(action)
        if policy is not None and policy != "blocked":
            return level
    return None


class PermissionChecker:
    def __init__(
        self,
        registry: CapabilityRegistry,
        state_manager: CapabilityStateManager,
        audit_client: AuditLogClient,
    ):
        self._registry = registry
        self._state = state_manager
        self._audit = audit_client

    async def check_permission(
        self,
        capability_id: str,
        action: str,
        job_ctx: JobContext,
        dependency_mode: str | None = None,
    ) -> PermissionResult:
        """Check whether a capability may perform a requested action.

        Implements the permission check algorithm from Trust Profile S8:

        1. Look up capability in registry. If not found -> blocked("unknown_capability")
        2. Check effective WAL from state. If -1 -> blocked("suspended")
        3. Look up action policy at current WAL level.
           - If action not in policies for this level -> blocked("action_not_permitted")
           - If policy is the string "blocked" -> blocked("action_not_permitted")
        4. If policy has cost_gate_usd and job_ctx.est_cost exceeds it -> held("cost_approval")
        5. Evaluate review_gate:
           - "pre_action": if not job_ctx.human_approved -> held("pre_action"), else -> allowed
           - "pre_delivery": -> allowed with delivery_hold=True
           - "post_action": -> allowed (delivery_hold=False)
           - "on_accept": if not job_ctx.result_accepted -> held("on_accept"), else -> allowed
           - "none": -> allowed

        Every call emits a durable wal.permission_check event to the audit log.
        The gated action MUST NOT proceed until the audit event is fsync'd + ACK'd.
        """
        # 1. Registry lookup
        cap = self._registry.get(capability_id)
        if cap is None:
            result = PermissionResult(
                result="blocked",
                block_reason="unknown_capability",
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=0,
                required_level=None,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        # 2. Effective WAL
        try:
            effective_wal = self._state.get_effective_wal(capability_id)
        except KeyError:
            result = PermissionResult(
                result="blocked",
                block_reason="unknown_capability",
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=0,
                required_level=None,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        if effective_wal == -1:
            required_level = _compute_required_level(cap, action)
            result = PermissionResult(
                result="blocked",
                block_reason="suspended",
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=-1,
                required_level=required_level,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        # 3. Action policy lookup
        required_level = _compute_required_level(cap, action)
        level_policies = cap["action_policies"].get(str(effective_wal), {})
        policy = level_policies.get(action) if isinstance(level_policies, dict) else None

        if policy is None or policy == "blocked":
            result = PermissionResult(
                result="blocked",
                block_reason="action_not_permitted",
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=effective_wal,
                required_level=required_level,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        # policy is a dict with review_gate, cost_gate_usd, retry_policy

        # 4. Cost gate (checked BEFORE review gate)
        cost_gate = policy.get("cost_gate_usd")
        if cost_gate is not None and job_ctx.est_cost is not None:
            if job_ctx.est_cost > cost_gate:
                result = PermissionResult(
                    result="held",
                    hold_type="cost_approval",
                    dependency_mode=dependency_mode,
                    capability_id=capability_id,
                    requested_action=action,
                    current_level=effective_wal,
                    required_level=required_level,
                )
                await self._emit_and_attach(result, job_ctx)
                return result

        # 5. Review gate
        review_gate = policy.get("review_gate", "none")

        if review_gate == "pre_action":
            if not job_ctx.human_approved:
                result = PermissionResult(
                    result="held",
                    hold_type="pre_action",
                    dependency_mode=dependency_mode,
                    capability_id=capability_id,
                    requested_action=action,
                    current_level=effective_wal,
                    required_level=required_level,
                )
                await self._emit_and_attach(result, job_ctx)
                return result
            result = PermissionResult(
                result="allowed",
                delivery_hold=False,
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=effective_wal,
                required_level=required_level,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        if review_gate == "pre_delivery":
            result = PermissionResult(
                result="allowed",
                delivery_hold=True,
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=effective_wal,
                required_level=required_level,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        if review_gate == "post_action":
            result = PermissionResult(
                result="allowed",
                delivery_hold=False,
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=effective_wal,
                required_level=required_level,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        if review_gate == "on_accept":
            if not job_ctx.result_accepted:
                result = PermissionResult(
                    result="held",
                    hold_type="on_accept",
                    dependency_mode=dependency_mode,
                    capability_id=capability_id,
                    requested_action=action,
                    current_level=effective_wal,
                    required_level=required_level,
                )
                await self._emit_and_attach(result, job_ctx)
                return result
            result = PermissionResult(
                result="allowed",
                delivery_hold=False,
                dependency_mode=dependency_mode,
                capability_id=capability_id,
                requested_action=action,
                current_level=effective_wal,
                required_level=required_level,
            )
            await self._emit_and_attach(result, job_ctx)
            return result

        # "none" or any other value -> allowed, no hold
        result = PermissionResult(
            result="allowed",
            delivery_hold=False,
            dependency_mode=dependency_mode,
            capability_id=capability_id,
            requested_action=action,
            current_level=effective_wal,
            required_level=required_level,
        )
        await self._emit_and_attach(result, job_ctx)
        return result

    async def _emit_and_attach(
        self,
        result: PermissionResult,
        job_ctx: JobContext,
    ) -> None:
        """Build and emit the durable audit event, then attach source_event_id to result."""
        event = event_wal_permission_check(
            job_id=job_ctx.job_id,
            capability_id=result.capability_id,
            requested_action=result.requested_action,
            current_level=result.current_level,
            required_level=result.required_level,
            result=result.result,
            delivery_hold=result.delivery_hold,
            hold_type=result.hold_type,
            block_reason=result.block_reason,
            dependency_mode=result.dependency_mode,
            wal_level=result.current_level,
        )
        result.source_event_id = event["source_event_id"]
        await self._audit.emit_durable(event)
