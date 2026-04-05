"""Egress proxy for Phase 6C: Network-level enforcement layer.

The EgressProxy is a request validator that the worker adapter calls before
making any outbound request. It enforces the allowlist declared in the worker's
manifest (as validated by 6A and bound by Spec 4's egress policy).

This is a code-level gate (v1). A true network proxy (iptables/envoy) is v2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from sandbox_blueprint import SandboxBlueprint

if TYPE_CHECKING:
    from audit_client import AuditLogClient
    from egress_rate_limiter import EgressRateLimiter


# Default Ollama host:port — always reachable for local routing
DEFAULT_OLLAMA_HOST = "127.0.0.1"
DEFAULT_OLLAMA_PORT = 11434


@dataclass
class EgressPolicy:
    """Spec 4 egress policy binding for a governing capability.

    Built from the egress routes configuration. Captures which endpoints
    a capability is authorized to reach and the rate limit.
    """
    capability_id: str
    allowed_endpoints: list[str]    # ["host:port", ...]
    rate_limit_rpm: int = 30


@dataclass
class EgressDecision:
    """Result of an egress authorization check."""
    allowed: bool
    reason: str
    matched_rule: Optional[str] = None


class EgressProxy:
    """Request validator that enforces egress allowlists.

    Sits between worker containers and the outside world. Every outbound
    request must pass through authorize() before execution.

    Authorization rules (evaluated in order, first match wins):
      1. network_mode "none" → DENY all
      2. Target is local Ollama → ALLOW always
      3. Target in blueprint egress_allow AND in Spec 4 policy → ALLOW
      4. Target in blueprint egress_allow but NOT in Spec 4 policy → DENY
      5. Default → DENY
    """

    def __init__(
        self,
        blueprint: SandboxBlueprint,
        egress_policy: EgressPolicy,
        ollama_host: str = DEFAULT_OLLAMA_HOST,
        ollama_port: int = DEFAULT_OLLAMA_PORT,
        rate_limiter: Optional[EgressRateLimiter] = None,
        audit_client: Optional[AuditLogClient] = None,
    ):
        self._blueprint = blueprint
        self._policy = egress_policy
        self._ollama_host = ollama_host
        self._ollama_port = ollama_port
        self._rate_limiter = rate_limiter
        self._audit_client = audit_client
        self._events: list[dict] = []

    @property
    def blueprint(self) -> SandboxBlueprint:
        return self._blueprint

    @property
    def policy(self) -> EgressPolicy:
        return self._policy

    @property
    def events(self) -> list[dict]:
        """All recorded audit events (for testing/inspection)."""
        return list(self._events)

    def authorize(
        self, target_host: str, target_port: int, method: str
    ) -> EgressDecision:
        """Evaluate egress authorization for an outbound request.

        Returns an EgressDecision indicating whether the request is allowed.
        """
        # Rule 1: network_mode "none" → DENY all
        if self._blueprint.network_config.network_mode == "none":
            return EgressDecision(
                allowed=False,
                reason="network_mode_none",
                matched_rule=None,
            )

        # Rule 2: Local Ollama is always reachable
        if target_host == self._ollama_host and target_port == self._ollama_port:
            return EgressDecision(
                allowed=True,
                reason="ollama_always_allowed",
                matched_rule=f"ollama:{self._ollama_host}:{self._ollama_port}",
            )

        target_endpoint = f"{target_host}:{target_port}"

        # Check if target is in blueprint's egress_allow
        in_blueprint = target_endpoint in self._blueprint.network_config.egress_allow

        # Check if target is in Spec 4 egress policy
        in_policy = target_endpoint in self._policy.allowed_endpoints

        # Rule 3: In both blueprint and policy → ALLOW (after rate limit check)
        if in_blueprint and in_policy:
            if self._rate_limiter is not None:
                if not self._rate_limiter.check(
                    self._blueprint.blueprint_id, self._policy.rate_limit_rpm
                ):
                    return EgressDecision(
                        allowed=False,
                        reason="rate_limited",
                        matched_rule=None,
                    )
            return EgressDecision(
                allowed=True,
                reason="allowlist_match",
                matched_rule=target_endpoint,
            )

        # Rule 4: In blueprint but not in policy → DENY (drift)
        if in_blueprint and not in_policy:
            return EgressDecision(
                allowed=False,
                reason="manifest_allows_but_policy_denies",
                matched_rule=None,
            )

        # Rule 5: Default deny
        return EgressDecision(
            allowed=False,
            reason="default_deny",
            matched_rule=None,
        )

    def log_request(
        self, target_host: str, target_port: int, decision: EgressDecision
    ) -> None:
        """Emit audit event for every egress attempt (allowed or denied).

        Imports egress_events lazily to avoid circular imports.
        """
        from egress_events import event_egress_authorized, event_egress_denied

        if decision.allowed:
            evt = event_egress_authorized(
                blueprint_id=self._blueprint.blueprint_id,
                capability_id=self._blueprint.capability_id,
                target_host=target_host,
                target_port=target_port,
                method="egress_check",
                matched_rule=decision.matched_rule or "",
            )
        else:
            evt = event_egress_denied(
                blueprint_id=self._blueprint.blueprint_id,
                capability_id=self._blueprint.capability_id,
                target_host=target_host,
                target_port=target_port,
                method="egress_check",
                denial_reason=decision.reason,
            )

        self._events.append(evt)

    async def log_request_durable(
        self, target_host: str, target_port: int, decision: EgressDecision
    ) -> None:
        """Emit audit event and persist via the audit log writer.

        Like log_request(), but also sends the event through the
        AuditLogClient for durable persistence. Falls back to
        in-memory-only if no audit client is configured.

        The in-memory list is always updated (used by teardown_worker
        for egress summary counts).
        """
        from egress_events import event_egress_authorized, event_egress_denied

        if decision.allowed:
            evt = event_egress_authorized(
                blueprint_id=self._blueprint.blueprint_id,
                capability_id=self._blueprint.capability_id,
                target_host=target_host,
                target_port=target_port,
                method="egress_check",
                matched_rule=decision.matched_rule or "",
            )
        else:
            evt = event_egress_denied(
                blueprint_id=self._blueprint.blueprint_id,
                capability_id=self._blueprint.capability_id,
                target_host=target_host,
                target_port=target_port,
                method="egress_check",
                denial_reason=decision.reason,
            )

        self._events.append(evt)

        if self._audit_client is not None:
            await self._audit_client.emit_durable(evt)

    @property
    def audit_client(self) -> Optional[AuditLogClient]:
        """The audit client, if configured."""
        return self._audit_client
