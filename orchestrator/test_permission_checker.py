"""Tests for permission_checker.py and pipeline_evaluator.py (Phase 2B)."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from permission_checker import JobContext, PermissionChecker, PermissionResult
from pipeline_evaluator import PipelineEvaluator, PipelineResult
from test_helpers import MockAuditClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "capabilities.json")


def _make_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(config_path=CONFIG_PATH)
    reg.load()
    return reg


def _make_state(registry: CapabilityRegistry, tmp_path: str) -> CapabilityStateManager:
    state_path = os.path.join(tmp_path, "capabilities.state.json")
    mgr = CapabilityStateManager(state_path=state_path)
    mgr.initialize_from_registry(registry)
    return mgr


@pytest.fixture
def setup(tmp_path):
    """Provide registry, state manager, audit client, and permission checker."""
    registry = _make_registry()
    state = _make_state(registry, str(tmp_path))
    audit = MockAuditClient()
    checker = PermissionChecker(registry, state, audit)
    return registry, state, audit, checker


# ---------------------------------------------------------------------------
# Permission Checker Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_governing_dispatch_cloud_wal0(setup):
    """dispatch_cloud on route.cloud.claude at WAL-0: allowed, delivery_hold=True (pre_delivery)."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-001")

    result = await checker.check_permission("route.cloud.claude", "dispatch_cloud", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is True
    assert result.current_level == 0

    # Verify audit event
    assert len(audit.events) == 1
    evt = audit.events[0]
    assert evt["event_type"] == "wal.permission_check"
    assert evt["payload"]["capability_id"] == "route.cloud.claude"
    assert evt["payload"]["requested_action"] == "dispatch_cloud"
    assert evt["payload"]["result"] == "allowed"
    assert evt["payload"]["delivery_hold"] is True


@pytest.mark.asyncio
async def test_governing_select_model_wal0(setup):
    """select_model on route.cloud.claude at WAL-0: held (pre_action, human_approved=False)."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-002")

    result = await checker.check_permission("route.cloud.claude", "select_model", ctx)

    assert result.result == "held"
    assert result.hold_type == "pre_action"


@pytest.mark.asyncio
async def test_governing_select_model_wal0_approved(setup):
    """select_model on route.cloud.claude at WAL-0 with human_approved=True: allowed."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-003", human_approved=True)

    result = await checker.check_permission("route.cloud.claude", "select_model", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is False


@pytest.mark.asyncio
async def test_governing_format_result_blocked_wal0(setup):
    """format_result on route.cloud.claude at WAL-0: blocked (action_not_permitted)."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-004")

    result = await checker.check_permission("route.cloud.claude", "format_result", ctx)

    assert result.result == "blocked"
    assert result.block_reason == "action_not_permitted"


@pytest.mark.asyncio
async def test_governing_auto_retry_blocked_wal0(setup):
    """auto_retry on route.cloud.claude at WAL-0: blocked."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-005")

    result = await checker.check_permission("route.cloud.claude", "auto_retry", ctx)

    assert result.result == "blocked"
    assert result.block_reason == "action_not_permitted"


@pytest.mark.asyncio
async def test_suspended_capability(setup):
    """Suspended capability -> blocked with block_reason 'suspended'."""
    registry, state, audit, checker = setup

    # Manually suspend route.cloud.claude
    entry = state.get("route.cloud.claude")
    entry["status"] = "suspended"
    entry["effective_wal_level"] = -1
    state.save()

    ctx = JobContext(job_id="job-006")
    result = await checker.check_permission("route.cloud.claude", "dispatch_cloud", ctx)

    assert result.result == "blocked"
    assert result.block_reason == "suspended"
    assert result.current_level == -1


@pytest.mark.asyncio
async def test_unknown_capability(setup):
    """Unknown capability -> blocked with block_reason 'unknown_capability'."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-007")

    result = await checker.check_permission("nonexistent.cap", "dispatch_cloud", ctx)

    assert result.result == "blocked"
    assert result.block_reason == "unknown_capability"


@pytest.mark.asyncio
async def test_cost_gate_exceeded(setup):
    """dispatch_cloud on route.cloud.claude at WAL-0, est_cost 0.50 > gate 0.25: held."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-008", est_cost=0.50)

    result = await checker.check_permission("route.cloud.claude", "dispatch_cloud", ctx)

    assert result.result == "held"
    assert result.hold_type == "cost_approval"


@pytest.mark.asyncio
async def test_cost_gate_not_exceeded(setup):
    """dispatch_cloud on route.cloud.claude at WAL-0, est_cost 0.10 <= gate 0.25: allowed."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-009", est_cost=0.10)

    result = await checker.check_permission("route.cloud.claude", "dispatch_cloud", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is True  # pre_delivery gate still applies


@pytest.mark.asyncio
async def test_cost_gate_null_ignored(setup):
    """dispatch_local on route.local at WAL-0, cost_gate_usd is null, high est_cost ignored."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-010", est_cost=100.0)

    result = await checker.check_permission("route.local", "dispatch_local", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is True  # pre_delivery gate


@pytest.mark.asyncio
async def test_wal1_format_result_allowed(setup):
    """route.cloud.claude at WAL-1: format_result allowed with delivery_hold=True."""
    registry, state, audit, checker = setup
    state.set_effective_wal("route.cloud.claude", 1)

    ctx = JobContext(job_id="job-011")
    result = await checker.check_permission("route.cloud.claude", "format_result", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is True
    assert result.current_level == 1


@pytest.mark.asyncio
async def test_wal1_auto_retry_allowed(setup):
    """route.cloud.claude at WAL-1: auto_retry allowed (review_gate: none)."""
    registry, state, audit, checker = setup
    state.set_effective_wal("route.cloud.claude", 1)

    ctx = JobContext(job_id="job-012")
    result = await checker.check_permission("route.cloud.claude", "auto_retry", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is False


@pytest.mark.asyncio
async def test_wal2_deliver_result_post_action(setup):
    """route.cloud.claude at WAL-2: deliver_result allowed, delivery_hold=False (post_action)."""
    registry, state, audit, checker = setup
    state.set_effective_wal("route.cloud.claude", 2)

    ctx = JobContext(job_id="job-013")
    result = await checker.check_permission("route.cloud.claude", "deliver_result", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is False


@pytest.mark.asyncio
async def test_wal2_select_model_none_gate(setup):
    """route.cloud.claude at WAL-2: select_model allowed, delivery_hold=False (none gate)."""
    registry, state, audit, checker = setup
    state.set_effective_wal("route.cloud.claude", 2)

    ctx = JobContext(job_id="job-014")
    result = await checker.check_permission("route.cloud.claude", "select_model", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is False


@pytest.mark.asyncio
async def test_write_memory_blocked_wal0(setup):
    """write_memory on memory.write at WAL-0: blocked."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-015")

    result = await checker.check_permission("memory.write", "write_memory", ctx)

    assert result.result == "blocked"
    assert result.block_reason == "action_not_permitted"


@pytest.mark.asyncio
async def test_write_memory_on_accept_wal1(setup):
    """memory.write at WAL-1, result_accepted=False: held (on_accept)."""
    registry, state, audit, checker = setup
    state.set_effective_wal("memory.write", 1)

    ctx = JobContext(job_id="job-016", result_accepted=False)
    result = await checker.check_permission("memory.write", "write_memory", ctx)

    assert result.result == "held"
    assert result.hold_type == "on_accept"


@pytest.mark.asyncio
async def test_write_memory_on_accept_wal1_accepted(setup):
    """memory.write at WAL-1, result_accepted=True: allowed."""
    registry, state, audit, checker = setup
    state.set_effective_wal("memory.write", 1)

    ctx = JobContext(job_id="job-017", result_accepted=True)
    result = await checker.check_permission("memory.write", "write_memory", ctx)

    assert result.result == "allowed"
    assert result.delivery_hold is False


@pytest.mark.asyncio
async def test_audit_event_payload_complete(setup):
    """Verify audit event has ALL required payload fields."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-018")

    await checker.check_permission("route.cloud.claude", "dispatch_cloud", ctx)

    assert len(audit.events) == 1
    payload = audit.events[0]["payload"]

    required_fields = {
        "capability_id",
        "job_id",
        "requested_action",
        "current_level",
        "required_level",
        "result",
        "delivery_hold",
        "hold_type",
        "block_reason",
        "dependency_mode",
    }
    assert required_fields == set(payload.keys())


# ---------------------------------------------------------------------------
# Pipeline Evaluator Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_required_blocked(setup):
    """context.package suspended -> pipeline blocked (required dependency)."""
    registry, state, audit, checker = setup

    # Suspend context.package
    entry = state.get("context.package")
    entry["status"] = "suspended"
    entry["effective_wal_level"] = -1
    state.save()

    evaluator = PipelineEvaluator(checker)
    ctx = JobContext(job_id="job-019")

    result = await evaluator.evaluate_pipeline("route.cloud.claude", ctx, registry)

    assert result.allowed is False
    assert result.blocked_by == "context.package"


@pytest.mark.asyncio
async def test_pipeline_optional_blocked(setup):
    """memory.read suspended -> pipeline allowed (optional dependency skipped)."""
    registry, state, audit, checker = setup

    # Suspend memory.read
    entry = state.get("memory.read")
    entry["status"] = "suspended"
    entry["effective_wal_level"] = -1
    state.save()

    evaluator = PipelineEvaluator(checker)
    ctx = JobContext(job_id="job-020")

    result = await evaluator.evaluate_pipeline("route.cloud.claude", ctx, registry)

    assert result.allowed is True


@pytest.mark.asyncio
async def test_pipeline_best_effort_blocked(setup):
    """notify.watch suspended -> pipeline allowed (best_effort dependency skipped)."""
    registry, state, audit, checker = setup

    # Suspend notify.watch
    entry = state.get("notify.watch")
    entry["status"] = "suspended"
    entry["effective_wal_level"] = -1
    state.save()

    evaluator = PipelineEvaluator(checker)
    ctx = JobContext(job_id="job-021")

    result = await evaluator.evaluate_pipeline("route.cloud.claude", ctx, registry)

    assert result.allowed is True


@pytest.mark.asyncio
async def test_pipeline_all_pass(setup):
    """All auxiliary capabilities active -> pipeline allowed, all results present."""
    registry, state, audit, checker = setup

    evaluator = PipelineEvaluator(checker)
    ctx = JobContext(job_id="job-022")

    result = await evaluator.evaluate_pipeline("route.cloud.claude", ctx, registry)

    assert result.allowed is True
    # route.cloud.claude has 4 pipeline entries: context.package, memory.read, notify.phone, notify.watch
    assert len(result.results) == 4
    for r in result.results:
        assert r.result == "allowed"


@pytest.mark.asyncio
async def test_provider_check_one_suspended(setup):
    """One cloud provider suspended -> available list excludes it."""
    registry, state, audit, checker = setup

    # Suspend route.cloud.claude
    entry = state.get("route.cloud.claude")
    entry["status"] = "suspended"
    entry["effective_wal_level"] = -1
    state.save()

    evaluator = PipelineEvaluator(checker)
    available, suspended = await evaluator.check_providers("route.multi", state, registry)

    assert "route.cloud.claude" in suspended
    assert "route.cloud.claude" not in available
    assert "route.cloud.openai" in available
    assert "route.cloud.gemini" in available


@pytest.mark.asyncio
async def test_provider_check_all_suspended(setup):
    """All three cloud providers suspended -> ValueError raised."""
    registry, state, audit, checker = setup

    for cap_id in ("route.cloud.claude", "route.cloud.openai", "route.cloud.gemini"):
        entry = state.get(cap_id)
        entry["status"] = "suspended"
        entry["effective_wal_level"] = -1
    state.save()

    evaluator = PipelineEvaluator(checker)

    with pytest.raises(ValueError, match="all_providers_suspended"):
        await evaluator.check_providers("route.multi", state, registry)


@pytest.mark.asyncio
async def test_source_event_id_captured(setup):
    """PermissionResult.source_event_id matches the emitted audit event's source_event_id."""
    registry, state, audit, checker = setup
    ctx = JobContext(job_id="job-025")

    result = await checker.check_permission("route.cloud.claude", "dispatch_cloud", ctx)

    assert result.source_event_id != ""
    assert len(audit.events) == 1
    assert result.source_event_id == audit.events[0]["source_event_id"]
