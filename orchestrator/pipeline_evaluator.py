"""Pipeline evaluator: checks declared pipeline permissions for a governing capability.

Iterates auxiliary capabilities in declared order, calling check_permission for each.
Applies dependency_mode semantics (required/optional/best_effort) to determine
whether the overall pipeline may proceed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from permission_checker import JobContext, PermissionChecker, PermissionResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Result of evaluating all pipeline steps."""
    allowed: bool  # True if the pipeline permits proceeding
    results: list[PermissionResult] = field(default_factory=list)
    blocked_by: str | None = None  # capability_id that blocked, if any
    block_reason: str | None = None


class PipelineEvaluator:
    def __init__(self, permission_checker: PermissionChecker):
        self._checker = permission_checker

    async def evaluate_pipeline(
        self,
        governing_cap_id: str,
        job_ctx: JobContext,
        registry: CapabilityRegistry,
    ) -> PipelineResult:
        """Evaluate the declared pipeline for a governing capability.

        Process:
        1. Get the governing capability's declared_pipeline from the registry
        2. For each entry in declared order:
           a. Get the auxiliary capability_id and dependency_mode
           b. Determine the action to check (the one action that capability owns)
           c. Call check_permission(aux_cap_id, action, job_ctx, dependency_mode)
           d. Based on dependency_mode and result:
              - required + blocked/held -> pipeline halts, return blocked
              - optional + blocked/held -> skip, continue, log
              - best_effort + blocked/held -> skip, continue, log
        3. Return PipelineResult

        Most restrictive result wins: if any required step blocks, the whole pipeline blocks.
        """
        gov_cap = registry.get(governing_cap_id)
        if gov_cap is None:
            return PipelineResult(
                allowed=False,
                blocked_by=governing_cap_id,
                block_reason="unknown_governing_capability",
            )

        declared_pipeline = gov_cap.get("declared_pipeline") or []
        results: list[PermissionResult] = []

        for entry in declared_pipeline:
            aux_cap_id = entry["capability_id"]
            dependency_mode = entry["dependency_mode"]

            # Find the single action owned by this auxiliary capability
            actions = registry.get_actions_for(aux_cap_id)
            if len(actions) != 1:
                logger.warning(
                    "Auxiliary %s owns %d actions (expected 1), skipping",
                    aux_cap_id, len(actions),
                )
                continue

            action = next(iter(actions))

            perm_result = await self._checker.check_permission(
                capability_id=aux_cap_id,
                action=action,
                job_ctx=job_ctx,
                dependency_mode=dependency_mode,
            )
            results.append(perm_result)

            if perm_result.result in ("blocked", "held"):
                if dependency_mode == "required":
                    return PipelineResult(
                        allowed=False,
                        results=results,
                        blocked_by=aux_cap_id,
                        block_reason=perm_result.block_reason or perm_result.hold_type,
                    )
                # optional / best_effort: skip and continue
                logger.info(
                    "Pipeline step %s (%s) %s — skipping (%s dependency)",
                    aux_cap_id, action, perm_result.result, dependency_mode,
                )

        return PipelineResult(allowed=True, results=results)

    async def check_providers(
        self,
        governing_cap_id: str,
        state_manager: CapabilityStateManager,
        registry: CapabilityRegistry,
    ) -> tuple[list[str], list[str]]:
        """Check provider dependencies for route.multi.

        Returns:
            (available_providers, suspended_providers)

        If all providers suspended, raises ValueError("all_providers_suspended").
        """
        gov_cap = registry.get(governing_cap_id)
        if gov_cap is None:
            raise ValueError(f"Unknown governing capability: {governing_cap_id}")

        provider_deps = gov_cap.get("provider_dependencies") or []
        if not provider_deps:
            return ([], [])

        available: list[str] = []
        suspended: list[str] = []

        for provider_id in provider_deps:
            try:
                effective_wal = state_manager.get_effective_wal(provider_id)
            except KeyError:
                logger.warning("Provider %s not in state, treating as suspended", provider_id)
                suspended.append(provider_id)
                continue

            if effective_wal == -1:
                suspended.append(provider_id)
            else:
                available.append(provider_id)

        if not available:
            raise ValueError("all_providers_suspended")

        return (available, suspended)
