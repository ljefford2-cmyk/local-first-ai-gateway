"""Capability registry: loads, validates, and serves lookups from capabilities.json."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Canonical action enum
GOVERNING_ACTIONS = frozenset({
    "dispatch_local",
    "dispatch_cloud",
    "dispatch_multi",
    "select_model",
    "deliver_result",
    "format_result",
    "auto_retry",
})

AUXILIARY_ACTIONS = frozenset({
    "package_context",
    "read_memory",
    "write_memory",
    "send_notification",
    "queue_job",
})

ALL_ACTIONS = GOVERNING_ACTIONS | AUXILIARY_ACTIONS

VALID_CAPABILITY_TYPES = frozenset({"governing", "auxiliary", "operational"})

VALID_GATE_TYPES = frozenset({
    "none",
    "pre_action",
    "pre_delivery",
    "post_action",
    "on_accept",
    "cost_approval",
})

VALID_DEPENDENCY_MODES = frozenset({"required", "optional", "best_effort"})

SENTINEL_FAILURE_IDS = frozenset({
    "egress_config",
    "egress_connectivity",
    "worker_sandbox",
})

REQUIRED_FIELDS = {
    "capability_id",
    "capability_name",
    "capability_type",
    "desired_wal_level",
    "max_wal",
    "declared_pipeline",
    "provider_dependencies",
    "action_policies",
    "promotion_criteria",
}


class CapabilityRegistry:
    def __init__(self, config_path: str = "/var/drnt/config/capabilities.json"):
        self._config_path = config_path
        self._capabilities: dict[str, dict] = {}
        self._config_hash: str | None = None

    def load(self) -> None:
        """Load and validate capabilities.json. Raises ValueError on validation failure."""
        raw = Path(self._config_path).read_bytes()
        self._config_hash = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)

        if not isinstance(data, dict):
            raise ValueError("capabilities.json must be a JSON object")

        # First pass: basic field validation
        for cap_id, cap in data.items():
            self._validate_fields(cap_id, cap)

        # Second pass: cross-capability validation
        self._validate_exclusive_ownership(data)
        self._validate_references(data)

        self._capabilities = data

    @property
    def config_hash(self) -> str | None:
        return self._config_hash

    def get(self, capability_id: str) -> dict | None:
        return self._capabilities.get(capability_id)

    def get_all(self) -> dict[str, dict]:
        return dict(self._capabilities)

    def get_governing(self) -> list[str]:
        return [
            cid for cid, c in self._capabilities.items()
            if c["capability_type"] == "governing"
        ]

    def get_auxiliary(self) -> list[str]:
        return [
            cid for cid, c in self._capabilities.items()
            if c["capability_type"] == "auxiliary"
        ]

    def get_actions_for(self, capability_id: str) -> set[str]:
        cap = self._capabilities.get(capability_id)
        if cap is None:
            return set()
        actions: set[str] = set()
        for _level, policies in cap["action_policies"].items():
            if isinstance(policies, dict):
                actions.update(policies.keys())
        return actions

    def is_sentinel(self, failing_capability_id: str) -> bool:
        return failing_capability_id in SENTINEL_FAILURE_IDS

    # ---- Validation helpers ----

    def _validate_fields(self, cap_id: str, cap: dict) -> None:
        missing = REQUIRED_FIELDS - set(cap.keys())
        if missing:
            raise ValueError(f"{cap_id}: missing required fields: {missing}")

        if cap["capability_type"] not in VALID_CAPABILITY_TYPES:
            raise ValueError(
                f"{cap_id}: invalid capability_type '{cap['capability_type']}'"
            )

        desired = cap["desired_wal_level"]
        max_wal = cap["max_wal"]

        if not (0 <= desired <= 3):
            raise ValueError(f"{cap_id}: desired_wal_level {desired} out of range [0,3]")
        if not (0 <= max_wal <= 3):
            raise ValueError(f"{cap_id}: max_wal {max_wal} out of range [0,3]")
        if desired > max_wal:
            raise ValueError(
                f"{cap_id}: desired_wal_level ({desired}) exceeds max_wal ({max_wal})"
            )

        # Validate action_policies structure and contents
        policies = cap["action_policies"]
        for level_str in ("0", "1", "2", "3"):
            if level_str not in policies:
                raise ValueError(f"{cap_id}: action_policies missing WAL level '{level_str}'")

        for level_str, level_policies in policies.items():
            if not isinstance(level_policies, dict):
                raise ValueError(
                    f"{cap_id}: action_policies['{level_str}'] must be a dict"
                )
            for action_name, policy in level_policies.items():
                if action_name not in ALL_ACTIONS:
                    raise ValueError(
                        f"{cap_id}: unknown action '{action_name}' in action_policies"
                    )
                if isinstance(policy, dict) and "review_gate" in policy:
                    if policy["review_gate"] not in VALID_GATE_TYPES:
                        raise ValueError(
                            f"{cap_id}: invalid review_gate '{policy['review_gate']}' "
                            f"for action '{action_name}'"
                        )

        # Validate declared_pipeline dependency_modes
        for entry in cap.get("declared_pipeline") or []:
            mode = entry.get("dependency_mode")
            if mode not in VALID_DEPENDENCY_MODES:
                raise ValueError(
                    f"{cap_id}: invalid dependency_mode '{mode}' in declared_pipeline"
                )

    def _validate_exclusive_ownership(self, data: dict) -> None:
        governing_actions: set[str] = set()
        auxiliary_actions: set[str] = set()

        for cap_id, cap in data.items():
            actions = set()
            for _level, policies in cap["action_policies"].items():
                if isinstance(policies, dict):
                    actions.update(policies.keys())

            cap_type = cap["capability_type"]

            if cap_type == "governing":
                invalid = actions - GOVERNING_ACTIONS
                if invalid:
                    raise ValueError(
                        f"{cap_id}: governing capability owns non-governing "
                        f"actions: {invalid}"
                    )
                governing_actions.update(actions)

            elif cap_type == "auxiliary":
                invalid = actions - AUXILIARY_ACTIONS
                if invalid:
                    raise ValueError(
                        f"{cap_id}: auxiliary capability owns non-auxiliary "
                        f"actions: {invalid}"
                    )
                auxiliary_actions.update(actions)
            # operational can own either

        overlap = governing_actions & auxiliary_actions
        if overlap:
            raise ValueError(
                f"Exclusive ownership violation: actions {overlap} appear in both "
                f"governing and auxiliary capabilities"
            )

    def _validate_references(self, data: dict) -> None:
        all_ids = set(data.keys())
        auxiliary_ids = {
            cid for cid, c in data.items() if c["capability_type"] == "auxiliary"
        }
        governing_ids = {
            cid for cid, c in data.items() if c["capability_type"] == "governing"
        }

        for cap_id, cap in data.items():
            for entry in cap.get("declared_pipeline") or []:
                ref = entry["capability_id"]
                if ref not in auxiliary_ids:
                    raise ValueError(
                        f"{cap_id}: declared_pipeline references '{ref}' "
                        f"which is not a registered auxiliary capability"
                    )

            for dep in cap.get("provider_dependencies") or []:
                if dep not in governing_ids:
                    raise ValueError(
                        f"{cap_id}: provider_dependencies references '{dep}' "
                        f"which is not a registered governing capability"
                    )
