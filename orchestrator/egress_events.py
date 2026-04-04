"""Egress audit events for Phase 6C: Egress Proxy + Network Isolation.

Every egress attempt — allowed or denied — produces an audit event.
Events follow the build_event envelope pattern from orchestrator/events.py.
"""

from __future__ import annotations

from typing import Any

from events import build_event


def event_egress_authorized(
    blueprint_id: str,
    capability_id: str,
    target_host: str,
    target_port: int,
    method: str,
    matched_rule: str,
) -> dict[str, Any]:
    """Build an egress.authorized audit event.

    Emitted when an outbound request passes the egress proxy allowlist check.
    """
    return build_event(
        event_type="egress.authorized",
        job_id=None,
        source="egress_proxy",
        payload={
            "blueprint_id": blueprint_id,
            "capability_id": capability_id,
            "target_host": target_host,
            "target_port": target_port,
            "method": method,
            "matched_rule": matched_rule,
        },
    )


def event_egress_denied(
    blueprint_id: str,
    capability_id: str,
    target_host: str,
    target_port: int,
    method: str,
    denial_reason: str,
) -> dict[str, Any]:
    """Build an egress.denied audit event.

    Emitted when an outbound request is blocked by the egress proxy.
    denial_reason is one of:
      - "network_mode_none"
      - "manifest_allows_but_policy_denies"
      - "default_deny"
      - "rate_limited"
    """
    return build_event(
        event_type="egress.denied",
        job_id=None,
        source="egress_proxy",
        payload={
            "blueprint_id": blueprint_id,
            "capability_id": capability_id,
            "target_host": target_host,
            "target_port": target_port,
            "method": method,
            "denial_reason": denial_reason,
        },
    )
