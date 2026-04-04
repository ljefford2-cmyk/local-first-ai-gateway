"""Tests for the startup validation, reconciliation, and system event emission."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from startup import (
    GATE_STRICTNESS,
    SPEC_DEFAULT_GATES,
    StartupResult,
    get_ollama_model_info,
    reconcile,
    run,
    validate_gate_relaxation,
    validate_sensitivity_config,
)
from test_helpers import MockAuditClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def _copy_config(tmp_path: Path) -> tuple[Path, Path]:
    """Copy capabilities.json and sensitivity.json to a temp dir."""
    cap_src = CONFIG_DIR / "capabilities.json"
    sens_src = CONFIG_DIR / "sensitivity.json"
    cap_dst = tmp_path / "capabilities.json"
    sens_dst = tmp_path / "sensitivity.json"
    shutil.copy(cap_src, cap_dst)
    shutil.copy(sens_src, sens_dst)
    return cap_dst, sens_dst


def _load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def setup(tmp_path):
    """Provide temp copies of config files, registry, state manager, and mock audit client."""
    cap_path, sens_path = _copy_config(tmp_path)
    state_path = tmp_path / "state" / "capabilities.state.json"

    registry = CapabilityRegistry(config_path=str(cap_path))
    state_manager = CapabilityStateManager(state_path=str(state_path))
    audit = MockAuditClient()

    return {
        "registry": registry,
        "state_manager": state_manager,
        "audit": audit,
        "cap_path": cap_path,
        "sens_path": sens_path,
        "state_path": state_path,
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Clean First Startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_first_startup(setup):
    """No existing state file. Verify startup succeeds, state created, system.startup emitted."""
    s = setup
    assert not s["state_path"].exists()

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True
    assert result.config_hash is not None
    assert result.state_hash is not None

    # State file created
    assert s["state_path"].exists()

    # All 12 capabilities at WAL-0
    state_data = json.loads(s["state_path"].read_text(encoding="utf-8"))
    cap_ids = [k for k in state_data if k != "_meta"]
    assert len(cap_ids) == 12
    for cap_id in cap_ids:
        assert state_data[cap_id]["effective_wal_level"] == 0

    # system.startup event emitted with non-null hashes
    startup_events = [e for e in s["audit"].events if e["event_type"] == "system.startup"]
    assert len(startup_events) == 1
    payload = startup_events[0]["payload"]
    assert payload["config_hash"] is not None
    assert payload["state_hash"] is not None


@pytest.mark.asyncio
async def test_clean_startup_emits_no_wal_events(setup):
    """First startup (no previous hash). No wal.promoted or wal.demoted events."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True
    wal_events = [
        e for e in s["audit"].events
        if e["event_type"] in ("wal.promoted", "wal.demoted")
    ]
    assert len(wal_events) == 0

    # system.config_change IS emitted (first startup — previous hash is None != new hash)
    config_change = [e for e in s["audit"].events if e["event_type"] == "system.config_change"]
    assert len(config_change) == 1


# ---------------------------------------------------------------------------
# Unchanged Config Restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_config_restart(setup):
    """Run startup twice without modifying config. No WAL or config_change events on second run."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result1 = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )
    assert result1.success is True
    first_hash = result1.config_hash

    # Clear events for second run
    s["audit"].events.clear()

    # Recreate registry/state to simulate fresh process
    registry2 = CapabilityRegistry(config_path=str(s["cap_path"]))
    state2 = CapabilityStateManager(state_path=str(s["state_path"]))

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result2 = await run(
            registry=registry2,
            state_manager=state2,
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result2.success is True
    assert result2.config_hash == first_hash

    # No WAL events
    wal_events = [
        e for e in s["audit"].events
        if e["event_type"] in ("wal.promoted", "wal.demoted")
    ]
    assert len(wal_events) == 0

    # No config_change events (hash unchanged)
    config_changes = [e for e in s["audit"].events if e["event_type"] == "system.config_change"]
    assert len(config_changes) == 0

    # system.startup IS emitted with same config_hash
    startup_events = [e for e in s["audit"].events if e["event_type"] == "system.startup"]
    assert len(startup_events) == 1
    assert startup_events[0]["payload"]["config_hash"] == first_hash


# ---------------------------------------------------------------------------
# Promotion Via Config Edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promotion_via_config_edit(setup):
    """Edit config to bump desired_wal_level. Verify wal.promoted emitted."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result1 = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )
    assert result1.success is True

    # Edit config: promote route.cloud.claude to desired_wal_level 1
    config = _load_config(s["cap_path"])
    config["route.cloud.claude"]["desired_wal_level"] = 1
    _save_config(s["cap_path"], config)

    s["audit"].events.clear()

    registry2 = CapabilityRegistry(config_path=str(s["cap_path"]))
    state2 = CapabilityStateManager(state_path=str(s["state_path"]))

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result2 = await run(
            registry=registry2,
            state_manager=state2,
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result2.success is True

    # wal.promoted event emitted
    promoted = [e for e in s["audit"].events if e["event_type"] == "wal.promoted"]
    assert len(promoted) == 1
    assert promoted[0]["payload"]["capability_id"] == "route.cloud.claude"
    assert promoted[0]["payload"]["from_level"] == 0
    assert promoted[0]["payload"]["to_level"] == 1

    # system.config_change emitted
    config_changes = [e for e in s["audit"].events if e["event_type"] == "system.config_change"]
    assert len(config_changes) == 1

    # State reflects new level
    assert state2.get_effective_wal("route.cloud.claude") == 1


# ---------------------------------------------------------------------------
# Manual Demotion Via Config Edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_demotion_via_config_edit(setup):
    """Manually set effective to 2, then change config (desired stays 0). Verify wal.demoted."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result1 = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )
    assert result1.success is True

    # Manually set effective WAL to 2 for route.cloud.claude (simulating a previous promotion)
    s["state_manager"].set_effective_wal("route.cloud.claude", 2)

    # Edit the config file to force a config_hash change so reconciliation triggers.
    # Keep desired_wal_level: 0 for route.cloud.claude — the demotion comes from
    # effective(2) > desired(0) during reconciliation.
    # Promote a different capability (route.cloud.openai) to 1 to change the hash.
    config = _load_config(s["cap_path"])
    config["route.cloud.openai"]["desired_wal_level"] = 1
    _save_config(s["cap_path"], config)

    s["audit"].events.clear()

    registry2 = CapabilityRegistry(config_path=str(s["cap_path"]))
    state2 = CapabilityStateManager(state_path=str(s["state_path"]))

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result2 = await run(
            registry=registry2,
            state_manager=state2,
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result2.success is True

    # wal.demoted event
    demoted = [e for e in s["audit"].events if e["event_type"] == "wal.demoted"]
    assert len(demoted) == 1
    assert demoted[0]["payload"]["capability_id"] == "route.cloud.claude"
    assert demoted[0]["payload"]["from_level"] == 2
    assert demoted[0]["payload"]["to_level"] == 0
    assert demoted[0]["payload"]["trigger"] == "manual"


# ---------------------------------------------------------------------------
# Suspended Recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suspended_recovery(setup):
    """Suspended capability (effective=-1) with desired=0. Verify wal.promoted from -1 to 0."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result1 = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )
    assert result1.success is True

    # Suspend context.package
    s["state_manager"].suspend("context.package")

    # Force a config hash change so reconciliation triggers
    config = _load_config(s["cap_path"])
    config["route.cloud.claude"]["desired_wal_level"] = 1
    _save_config(s["cap_path"], config)

    s["audit"].events.clear()

    registry2 = CapabilityRegistry(config_path=str(s["cap_path"]))
    state2 = CapabilityStateManager(state_path=str(s["state_path"]))

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result2 = await run(
            registry=registry2,
            state_manager=state2,
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result2.success is True

    # wal.promoted from -1 to 0 for context.package
    promoted = [e for e in s["audit"].events if e["event_type"] == "wal.promoted"]
    context_promoted = [e for e in promoted if e["payload"]["capability_id"] == "context.package"]
    assert len(context_promoted) == 1
    assert context_promoted[0]["payload"]["from_level"] == -1
    assert context_promoted[0]["payload"]["to_level"] == 0

    # Status is now active
    assert state2.get_status("context.package") == "active"


# ---------------------------------------------------------------------------
# Validation Failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_failure_gate_relaxation(setup):
    """Relaxing dispatch_cloud at WAL-0 from pre_delivery to none should fail."""
    s = setup

    config = _load_config(s["cap_path"])
    # Set dispatch_cloud at WAL-0 to review_gate: "none" (relaxing from pre_delivery)
    config["route.cloud.claude"]["action_policies"]["0"]["dispatch_cloud"]["review_gate"] = "none"
    _save_config(s["cap_path"], config)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is False
    assert any("relaxes" in err.lower() or "relaxation" in err.lower() or "strictness" in err.lower() for err in result.errors)


@pytest.mark.asyncio
async def test_validation_failure_gate_relaxation_blocked_to_allowed(setup):
    """Making format_result non-blocked at WAL-0 (spec says blocked) should fail."""
    s = setup

    config = _load_config(s["cap_path"])
    # Change format_result at WAL-0 from "blocked" to a dict with review_gate
    config["route.local"]["action_policies"]["0"]["format_result"] = {
        "review_gate": "pre_delivery",
        "cost_gate_usd": None,
        "retry_policy": {"allowed": False, "max_retries": 0, "same_capability_only": True},
    }
    _save_config(s["cap_path"], config)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is False
    assert len(result.errors) >= 1


@pytest.mark.asyncio
async def test_validation_failure_desired_exceeds_max(setup):
    """desired_wal_level: 3 on a governing cap with max_wal: 2 should fail (caught by registry.load())."""
    s = setup

    config = _load_config(s["cap_path"])
    config["route.local"]["desired_wal_level"] = 3
    _save_config(s["cap_path"], config)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is False
    assert any("exceeds" in err or "max_wal" in err for err in result.errors)


@pytest.mark.asyncio
async def test_validation_failure_broken_pipeline_ref(setup):
    """Referencing a nonexistent auxiliary in declared_pipeline should fail."""
    s = setup

    config = _load_config(s["cap_path"])
    config["route.local"]["declared_pipeline"].append(
        {"capability_id": "nonexistent.cap", "dependency_mode": "required"}
    )
    _save_config(s["cap_path"], config)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is False
    assert any("nonexistent.cap" in err for err in result.errors)


@pytest.mark.asyncio
async def test_validation_failure_sensitivity_credential(setup):
    """Removing hardcoded: true from credential class should fail."""
    s = setup

    sens_data = _load_config(s["sens_path"])
    for entry in sens_data["sensitivity_classes"]:
        if entry["class"] == "credential":
            entry["hardcoded"] = False
    _save_config(s["sens_path"], sens_data)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is False
    assert any("credential" in err.lower() and "hardcoded" in err.lower() for err in result.errors)


@pytest.mark.asyncio
async def test_validation_failure_sensitivity_credential_action(setup):
    """Changing credential class action from strip to generalize should fail."""
    s = setup

    sens_data = _load_config(s["sens_path"])
    for entry in sens_data["sensitivity_classes"]:
        if entry["class"] == "credential":
            entry["action"] = "generalize"
    _save_config(s["sens_path"], sens_data)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is False
    assert any("credential" in err.lower() and "strip" in err.lower() for err in result.errors)


# ---------------------------------------------------------------------------
# Config Hash / State Hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_hash_in_startup_event(setup):
    """system.startup event payload has config_hash matching registry.config_hash."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True
    startup_events = [e for e in s["audit"].events if e["event_type"] == "system.startup"]
    assert len(startup_events) == 1
    assert startup_events[0]["payload"]["config_hash"] == s["registry"].config_hash


@pytest.mark.asyncio
async def test_state_hash_in_startup_event(setup):
    """system.startup event payload has state_hash matching state_manager.state_hash."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True
    startup_events = [e for e in s["audit"].events if e["event_type"] == "system.startup"]
    assert len(startup_events) == 1
    assert startup_events[0]["payload"]["state_hash"] == s["state_manager"].state_hash


@pytest.mark.asyncio
async def test_meta_stored_in_state(setup):
    """After startup, _meta exists in state with last_config_hash set."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True

    state_data = json.loads(s["state_path"].read_text(encoding="utf-8"))
    assert "_meta" in state_data
    assert state_data["_meta"]["last_config_hash"] == s["registry"].config_hash


# ---------------------------------------------------------------------------
# Reconciliation Details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciliation_multiple_capabilities(setup):
    """Edit config for 3 caps: one up, one down, one same. Verify correct WAL events."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result1 = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )
    assert result1.success is True

    # Manually set route.cloud.openai to effective WAL 2
    s["state_manager"].set_effective_wal("route.cloud.openai", 2)

    # Edit config:
    # - route.cloud.claude: desired 1 (was 0 -> promote)
    # - route.cloud.openai: desired stays 0 (effective is 2 -> demote)
    # - route.local: desired stays 0 (effective is 0 -> no change)
    config = _load_config(s["cap_path"])
    config["route.cloud.claude"]["desired_wal_level"] = 1
    _save_config(s["cap_path"], config)

    s["audit"].events.clear()

    registry2 = CapabilityRegistry(config_path=str(s["cap_path"]))
    state2 = CapabilityStateManager(state_path=str(s["state_path"]))

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result2 = await run(
            registry=registry2,
            state_manager=state2,
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result2.success is True
    assert result2.reconciliation_events == 2

    promoted = [e for e in s["audit"].events if e["event_type"] == "wal.promoted"]
    demoted = [e for e in s["audit"].events if e["event_type"] == "wal.demoted"]
    assert len(promoted) == 1
    assert len(demoted) == 1

    assert promoted[0]["payload"]["capability_id"] == "route.cloud.claude"
    assert demoted[0]["payload"]["capability_id"] == "route.cloud.openai"

    # No event for route.local
    all_wal_cap_ids = [e["payload"]["capability_id"] for e in s["audit"].events if e["event_type"] in ("wal.promoted", "wal.demoted")]
    assert "route.local" not in all_wal_cap_ids


@pytest.mark.asyncio
async def test_reconciliation_preserves_other_state(setup):
    """Outcomes for an unchanged capability are preserved across restart."""
    s = setup

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result1 = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )
    assert result1.success is True

    # Add some outcomes to route.local
    s["state_manager"].record_outcome("route.local", {
        "disposition": "accepted",
        "timestamp": "2026-01-01T00:00:00Z",
    })

    s["audit"].events.clear()

    # Restart with same config (no changes)
    registry2 = CapabilityRegistry(config_path=str(s["cap_path"]))
    state2 = CapabilityStateManager(state_path=str(s["state_path"]))

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result2 = await run(
            registry=registry2,
            state_manager=state2,
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result2.success is True

    # Outcomes still present
    outcomes = state2.get_outcomes("route.local")
    assert len(outcomes) == 1
    assert outcomes[0]["disposition"] == "accepted"


# ---------------------------------------------------------------------------
# Gate Validation Details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tightened_gate_accepted(setup):
    """Tightening dispatch_cloud at WAL-0 from pre_delivery to pre_action should succeed."""
    s = setup

    config = _load_config(s["cap_path"])
    config["route.cloud.claude"]["action_policies"]["0"]["dispatch_cloud"]["review_gate"] = "pre_action"
    _save_config(s["cap_path"], config)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True


@pytest.mark.asyncio
async def test_blocked_is_tightest(setup):
    """Setting dispatch_cloud at WAL-0 to blocked should succeed (blocked is always acceptable)."""
    s = setup

    config = _load_config(s["cap_path"])
    config["route.cloud.claude"]["action_policies"]["0"]["dispatch_cloud"] = "blocked"
    _save_config(s["cap_path"], config)

    with patch("startup.get_ollama_model_info", return_value=(None, None)):
        result = await run(
            registry=s["registry"],
            state_manager=s["state_manager"],
            audit_client=s["audit"],
            sensitivity_config_path=str(s["sens_path"]),
        )

    assert result.success is True
