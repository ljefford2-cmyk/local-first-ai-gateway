"""Startup validation, reconciliation, and system event emission.

Runs before the orchestrator accepts any jobs. Validates configuration,
reconciles desired vs effective WAL levels, checks Ollama model version,
and emits system events for the audit trail.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from audit_client import AuditLogClient
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from events import (
    event_system_config_change,
    event_system_model_change,
    event_system_startup,
    event_wal_demoted,
    event_wal_promoted,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spec default gates per WAL level — the minimum strictness.
# Format: {action: {wal_level_str: review_gate_value}}
# "blocked" means the action is not available at that level.
# ---------------------------------------------------------------------------
SPEC_DEFAULT_GATES: dict[str, dict[str, str]] = {
    # Governing actions
    "dispatch_local": {"0": "pre_delivery", "1": "pre_delivery", "2": "post_action"},
    "dispatch_cloud": {"0": "pre_delivery", "1": "pre_delivery", "2": "post_action"},
    "dispatch_multi": {"0": "pre_delivery", "1": "pre_delivery", "2": "post_action"},
    "select_model": {"0": "pre_action", "1": "pre_action", "2": "none"},
    "deliver_result": {"0": "pre_delivery", "1": "pre_delivery", "2": "post_action"},
    "format_result": {"0": "blocked", "1": "pre_delivery", "2": "pre_delivery"},
    "auto_retry": {"0": "blocked", "1": "none", "2": "none"},
    # Auxiliary actions
    "package_context": {"0": "none", "1": "none", "2": "none", "3": "none"},
    "read_memory": {"0": "none", "1": "none", "2": "none", "3": "none"},
    "write_memory": {"0": "blocked", "1": "on_accept", "2": "none", "3": "none"},
    "send_notification": {"0": "none", "1": "none", "2": "none", "3": "none"},
    "queue_job": {"0": "none", "1": "none", "2": "none", "3": "none"},
}

# Gate strictness ordering (most strict -> least strict)
GATE_STRICTNESS = {
    "blocked": 0,
    "pre_action": 1,
    "cost_approval": 2,
    "pre_delivery": 3,
    "on_accept": 4,
    "post_action": 5,
    "none": 6,
}


@dataclass
class StartupResult:
    success: bool
    errors: list[str] = field(default_factory=list)
    config_hash: str | None = None
    state_hash: str | None = None
    ollama_model: str | None = None
    reconciliation_events: int = 0


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_sensitivity_config(sensitivity_config_path: str) -> list[str]:
    """Validate that sensitivity.json has credential class with hardcoded: true, action: strip.

    Returns list of error strings. Empty list = valid.
    """
    errors: list[str] = []
    try:
        raw = Path(sensitivity_config_path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        return [f"Cannot load sensitivity config: {e}"]

    classes = data.get("sensitivity_classes", [])
    credential_entry = None
    for entry in classes:
        if entry.get("class") == "credential":
            credential_entry = entry
            break

    if credential_entry is None:
        return ["sensitivity.json: missing 'credential' class entry"]

    if credential_entry.get("hardcoded") is not True:
        errors.append(
            "sensitivity.json: credential class must have hardcoded: true"
        )

    if credential_entry.get("action") != "strip":
        errors.append(
            "sensitivity.json: credential class must have action: strip"
        )

    return errors


def validate_gate_relaxation(registry: CapabilityRegistry) -> list[str]:
    """Check that no capability config relaxes gates beyond spec defaults.

    Returns list of error strings. Empty list = valid.
    """
    errors: list[str] = []

    for cap_id, cap in registry.get_all().items():
        policies = cap.get("action_policies", {})
        for level_str, level_policies in policies.items():
            if not isinstance(level_policies, dict):
                continue
            for action_name, policy in level_policies.items():
                # Look up spec default for this action at this level
                spec_defaults_for_action = SPEC_DEFAULT_GATES.get(action_name)
                if spec_defaults_for_action is None:
                    continue
                spec_default = spec_defaults_for_action.get(level_str)
                if spec_default is None:
                    # No spec default for this action at this level — skip
                    continue

                # Determine the config gate value
                if policy == "blocked":
                    config_gate = "blocked"
                elif isinstance(policy, dict):
                    config_gate = policy.get("review_gate", "none")
                else:
                    continue

                spec_strictness = GATE_STRICTNESS.get(spec_default)
                config_strictness = GATE_STRICTNESS.get(config_gate)

                if spec_strictness is None or config_strictness is None:
                    continue

                if config_strictness > spec_strictness:
                    errors.append(
                        f"{cap_id}: action '{action_name}' at WAL-{level_str} "
                        f"has gate '{config_gate}' (strictness {config_strictness}) "
                        f"which relaxes spec default '{spec_default}' "
                        f"(strictness {spec_strictness})"
                    )

    return errors


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


async def reconcile(
    registry: CapabilityRegistry,
    state_manager: CapabilityStateManager,
    audit_client: AuditLogClient,
    previous_config_hash: str | None,
) -> int:
    """Reconcile desired vs effective WAL levels after config change.

    For each capability:
    - desired > effective -> emit wal.promoted, set effective = desired
    - desired < effective -> emit wal.demoted(trigger: manual), set effective = desired
    - desired == effective -> no change
    - Suspended recovery: effective = -1, desired = 0 -> emit wal.promoted(from: -1, to: 0)

    Returns count of WAL events emitted.
    """
    events_emitted = 0

    for cap_id, cap_config in registry.get_all().items():
        desired = cap_config["desired_wal_level"]
        effective = state_manager.get_effective_wal(cap_id)

        if desired == effective:
            continue

        capability_name = cap_config["capability_name"]

        if desired > effective:
            event = event_wal_promoted(
                capability_id=cap_id,
                capability_name=capability_name,
                from_level=effective,
                to_level=desired,
            )
            await audit_client.emit_durable(event)
            state_manager.set_effective_wal(cap_id, desired)
            # Handle suspended recovery: restore status to active
            if effective == -1:
                entry = state_manager.get(cap_id)
                if entry is not None:
                    entry["status"] = "active"
                    state_manager.save()
            events_emitted += 1
        else:
            # desired < effective — manual demotion via config edit
            event = event_wal_demoted(
                capability_id=cap_id,
                capability_name=capability_name,
                from_level=effective,
                to_level=desired,
                trigger="manual",
            )
            await audit_client.emit_durable(event)
            state_manager.demote(cap_id, desired)
            state_manager.reset_counters(cap_id)
            events_emitted += 1

    return events_emitted


# ---------------------------------------------------------------------------
# Ollama model info
# ---------------------------------------------------------------------------


async def get_ollama_model_info(
    ollama_url: str | None = None,
) -> tuple[str | None, str | None]:
    """Query Ollama's /api/tags endpoint to get the current model name and digest.

    Returns (model_name, model_digest) or (None, None) on failure.
    """
    url = ollama_url or os.environ.get("OLLAMA_URL", "http://ollama:11434")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = data.get("models", [])
            if not models:
                logger.warning("Ollama returned no models")
                return (None, None)
            first = models[0]
            return (first.get("name"), first.get("digest"))
    except Exception as e:
        logger.warning("Could not query Ollama model info: %s", e)
        return (None, None)


# ---------------------------------------------------------------------------
# Main startup sequence
# ---------------------------------------------------------------------------


async def run(
    registry: CapabilityRegistry,
    state_manager: CapabilityStateManager,
    audit_client: AuditLogClient,
    sensitivity_config_path: str,
    ollama_url: str | None = None,
) -> StartupResult:
    """Execute the full startup sequence.

    Sequence:
    1. Load capabilities.json (registry.load())
    2. Validate (gate relaxation, sensitivity config)
    3. Compute config_hash (already done in load())
    4. Load capabilities.state.json (or initialize fresh if missing)
    5. Compute state_hash
    6. Compare config_hash -> reconcile WAL levels
    7. Check Ollama model version
    8. Write reconciled state
    9. Emit system.startup event
    10. Return StartupResult
    """
    # Step 1 — Load registry
    try:
        registry.load()
    except ValueError as e:
        return StartupResult(success=False, errors=[f"Registry validation failed: {e}"])

    # Step 2 — Additional validation
    gate_errors = validate_gate_relaxation(registry)
    if gate_errors:
        return StartupResult(success=False, errors=gate_errors)

    sensitivity_errors = validate_sensitivity_config(sensitivity_config_path)
    if sensitivity_errors:
        return StartupResult(success=False, errors=sensitivity_errors)

    # Step 4 — Load or initialize state
    state_file = Path(state_manager._state_path)
    if state_file.exists():
        state_manager.load()
    else:
        state_manager.initialize_from_registry(registry)

    # Step 6 — Config hash comparison and reconciliation
    # _meta is stored as a top-level key in the state dict, managed entirely here.
    meta = state_manager._state.get("_meta", {})
    previous_config_hash = meta.get("last_config_hash")

    reconciliation_events = 0
    if previous_config_hash != registry.config_hash:
        reconciliation_events = await reconcile(
            registry, state_manager, audit_client, previous_config_hash
        )
        config_change_event = event_system_config_change(
            previous_hash=previous_config_hash,
            new_hash=registry.config_hash,
        )
        await audit_client.emit_durable(config_change_event)

    # Step 7 — Ollama model check
    ollama_model, ollama_digest = await get_ollama_model_info(ollama_url)

    previous_model = meta.get("last_ollama_model")
    previous_digest = meta.get("last_ollama_digest")

    if (
        previous_model is not None
        and ollama_model is not None
        and (previous_model != ollama_model or previous_digest != ollama_digest)
    ):
        # Model changed — all capabilities affected (classifier model impacts everything)
        affected = [c for c in registry.get_all().keys() if not c.startswith("_")]

        model_change_event = event_system_model_change(
            previous_model=previous_model,
            new_model=ollama_model,
            affected_capabilities=affected,
        )
        model_change_source_id = model_change_event["source_event_id"]
        await audit_client.emit_durable(model_change_event)

        from demotion_engine import DemotionEngine

        engine = DemotionEngine(registry, state_manager, audit_client)
        await engine.handle_model_change(
            affected, source_event_id=model_change_source_id
        )

    # Step 8 — Update meta and save
    state_manager._state["_meta"] = {
        "last_config_hash": registry.config_hash,
        "last_ollama_model": ollama_model,
        "last_ollama_digest": ollama_digest,
    }
    state_manager.save()

    # Step 9 — Emit system.startup
    startup_event = event_system_startup(
        config_hash=registry.config_hash,
        state_hash=state_manager.state_hash,
        ollama_model=ollama_model,
        ollama_model_version=ollama_digest,
        components_loaded=[
            "audit_log_writer",
            "capability_registry",
            "capability_state",
            "permission_checker",
            "demotion_engine",
            "context_packager",
        ],
    )
    await audit_client.emit_durable(startup_event)

    # Step 10 — Return
    return StartupResult(
        success=True,
        config_hash=registry.config_hash,
        state_hash=state_manager.state_hash,
        ollama_model=ollama_model,
        reconciliation_events=reconciliation_events,
    )
