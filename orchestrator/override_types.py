"""Override type definitions and sentinel-failure utilities for Phase 5."""

from __future__ import annotations

from enum import Enum


class OverrideType(str, Enum):
    cancel = "cancel"
    redirect = "redirect"
    modify = "modify"
    escalate = "escalate"


VALID_OVERRIDE_TARGETS = frozenset({"routing", "response", "wal_action"})

SENTINEL_FAILURE_IDS = frozenset({"egress_config", "egress_connectivity", "worker_sandbox"})


def is_sentinel_failure(failing_capability_id: str | None) -> bool:
    """Return True if the failing_capability_id is a sentinel (infrastructure
    failure, not a routing judgment error). Sentinel failures suppress WAL
    demotion on cancel/redirect overrides."""
    return failing_capability_id is not None and failing_capability_id in SENTINEL_FAILURE_IDS


SPAWNS_SUCCESSOR = frozenset({OverrideType.redirect, OverrideType.escalate})

CONDITIONAL_DEMOTION_TYPES = frozenset({OverrideType.cancel, OverrideType.redirect})

TERMINAL_STATES = frozenset({"delivered", "failed", "revoked"})
