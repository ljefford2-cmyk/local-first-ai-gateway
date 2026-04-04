"""Phase 7E unit tests — WAL Temporal Decay.

Covers: DecayPolicy, DecayConfig loading/validation, DecayEvaluator
single-pass and multi-pass evaluation, counter resets, event emission,
per-capability overrides, and interaction with existing demotion triggers.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from decay_evaluator import (
    DecayAction,
    DecayConfig,
    DecayEvaluator,
    DecayPolicy,
    load_decay_config,
)
from test_helpers import MockAuditClient


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _past_iso(days_ago: int = 0, hours_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Helpers: minimal capabilities config & state setup
# ---------------------------------------------------------------------------

_MINIMAL_CAP = {
    "capability_name": "Test Cap",
    "capability_type": "governing",
    "desired_wal_level": 0,
    "max_wal": 3,
    "declared_pipeline": [],
    "provider_dependencies": None,
    "action_policies": {
        "0": {"dispatch_local": {"review_gate": "none"}},
        "1": {"dispatch_local": {"review_gate": "none"}},
        "2": {"dispatch_local": {"review_gate": "none"}},
        "3": {"dispatch_local": {"review_gate": "none"}},
    },
    "promotion_criteria": {},
}


def _make_cap_config(*cap_ids: str) -> dict:
    """Build a minimal capabilities.json dict."""
    caps = {}
    for cid in cap_ids:
        caps[cid] = {**_MINIMAL_CAP, "capability_id": cid}
    return caps


def _write_json(path: str, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f)


def _setup(
    cap_ids: list[str],
    wal_levels: dict[str, int] | None = None,
    outcomes: dict[str, list[dict]] | None = None,
    decay_policy: dict | None = "default",
    decay_overrides: dict | None = None,
):
    """Create registry, state, config, and evaluator.

    Returns (evaluator, state_manager, audit_client).
    """
    tmpdir = tempfile.mkdtemp()
    config_path = os.path.join(tmpdir, "capabilities.json")
    state_path = os.path.join(tmpdir, "capabilities.state.json")

    cap_data = _make_cap_config(*cap_ids)
    _write_json(config_path, cap_data)

    registry = CapabilityRegistry(config_path)
    registry.load()

    state = CapabilityStateManager(state_path)
    state.initialize_from_registry(registry)

    # Set WAL levels
    if wal_levels:
        for cid, level in wal_levels.items():
            state.set_effective_wal(cid, level)

    # Inject outcomes into ring buffer
    if outcomes:
        for cid, outcome_list in outcomes.items():
            for o in outcome_list:
                state.record_outcome(cid, o)

    # Build decay config
    if decay_policy == "default":
        config_data = {
            "decay_policy": {
                "wal_1": {"window_days": 90, "min_outcomes": 10},
                "wal_2": {"window_days": 60, "min_outcomes": 25},
                "wal_3": {"window_days": 30, "min_outcomes": 50},
            },
        }
    elif decay_policy is None:
        config_data = {}
    else:
        config_data = {"decay_policy": decay_policy}

    if decay_overrides:
        config_data["decay_overrides"] = decay_overrides

    dc = load_decay_config(config_data)
    evaluator = DecayEvaluator(dc, state)
    audit = MockAuditClient()

    return evaluator, state, audit


def _make_outcome(days_ago: int = 0, disposition: str = "accepted") -> dict:
    """Build a minimal outcome dict with timestamp."""
    return {
        "timestamp": _past_iso(days_ago=days_ago),
        "disposition": disposition,
        "job_id": "j-test",
    }


# ===========================================================================
# Tests
# ===========================================================================


# 1. WAL-0 capability is never evaluated for decay
def test_wal_0_never_evaluated():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 0})
    actions = _run(ev.evaluate(audit))
    assert actions == []
    assert state.get_effective_wal("cap.a") == 0


# 2. WAL-1 with >=10 outcomes in 90 days NOT demoted
def test_wal1_sufficient_outcomes_not_demoted():
    outcomes = [_make_outcome(days_ago=i) for i in range(10)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert actions == []
    assert state.get_effective_wal("cap.a") == 1


# 3. WAL-1 with <10 outcomes in 90 days IS demoted to WAL-0
def test_wal1_insufficient_outcomes_demoted():
    outcomes = [_make_outcome(days_ago=i) for i in range(9)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].from_level == 1
    assert actions[0].to_level == 0
    assert state.get_effective_wal("cap.a") == 0


# 4. WAL-2 with >=25 outcomes in 60 days NOT demoted
def test_wal2_sufficient_outcomes_not_demoted():
    outcomes = [_make_outcome(days_ago=i) for i in range(25)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 2}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert actions == []
    assert state.get_effective_wal("cap.a") == 2


# 5. WAL-2 with <25 outcomes in 60 days IS demoted to WAL-1 (not WAL-0)
def test_wal2_insufficient_outcomes_demoted_to_wal1():
    outcomes = [_make_outcome(days_ago=i) for i in range(24)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 2}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].from_level == 2
    assert actions[0].to_level == 1
    assert state.get_effective_wal("cap.a") == 1


# 6. WAL-3 with >=50 outcomes in 30 days NOT demoted
def test_wal3_sufficient_outcomes_not_demoted():
    outcomes = [_make_outcome(days_ago=i % 30) for i in range(50)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 3}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert actions == []
    assert state.get_effective_wal("cap.a") == 3


# 7. WAL-3 with <50 outcomes in 30 days IS demoted to WAL-2
def test_wal3_insufficient_outcomes_demoted_to_wal2():
    outcomes = [_make_outcome(days_ago=i % 30) for i in range(49)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 3}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].from_level == 3
    assert actions[0].to_level == 2
    assert state.get_effective_wal("cap.a") == 2


# 8. Decay demotes by exactly one level per evaluation cycle
def test_decay_demotes_exactly_one_level():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 3})
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].from_level == 3
    assert actions[0].to_level == 2
    # Still WAL-2, not WAL-0
    assert state.get_effective_wal("cap.a") == 2


# 9. Outcomes outside the window are not counted
def test_outcomes_outside_window_not_counted():
    # WAL-1 window is 90 days — place all outcomes at 100 days ago
    outcomes = [_make_outcome(days_ago=100) for _ in range(20)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].outcomes_in_window == 0


# 10. Outcomes inside the window are counted
def test_outcomes_inside_window_counted():
    outcomes = [_make_outcome(days_ago=5) for _ in range(10)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    assert actions == []  # 10 >= 10, no demotion


# 11. Boundary: outcome exactly at window edge is counted (inclusive)
def test_boundary_outcome_at_window_edge_counted():
    # WAL-1 window is 90 days. Place 10 outcomes just barely inside the
    # window (89d 23h 59m) to verify the >= comparison includes the edge.
    # Exact-to-the-microsecond boundary is unreliable due to clock drift
    # between outcome creation and evaluation, so we test just inside.
    ts = (datetime.now(timezone.utc) - timedelta(days=89, hours=23, minutes=59)).isoformat(timespec="microseconds")
    outcomes = [{"timestamp": ts, "disposition": "accepted", "job_id": "j-edge"} for _ in range(10)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate(audit))
    # Just inside boundary -> counted, 10 >= 10 -> no demotion
    assert actions == []

    # Now verify that outcomes clearly outside (91 days) are NOT counted
    ts_outside = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat(timespec="microseconds")
    outcomes_outside = [{"timestamp": ts_outside, "disposition": "accepted", "job_id": "j-out"} for _ in range(10)]
    ev2, state2, audit2 = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes_outside}
    )
    actions2 = _run(ev2.evaluate(audit2))
    assert len(actions2) == 1  # Outside window -> demoted


# 12. Empty ring buffer triggers decay
def test_empty_ring_buffer_triggers_decay():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 1})
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].outcomes_in_window == 0
    assert state.get_effective_wal("cap.a") == 0


# 13. Decay emits wal.demoted event with trigger "temporal_decay"
def test_decay_emits_wal_demoted_event():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 1})
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert len(events) == 1
    assert events[0]["payload"]["trigger"] == "temporal_decay"


# 14. Decay event includes correct window_days, outcomes_in_window, min_required
def test_decay_event_fields():
    outcomes = [_make_outcome(days_ago=5) for _ in range(3)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["window_days"] == 90
    assert p["outcomes_in_window"] == 3
    assert p["min_required"] == 10


# 15. Decay event includes last_outcome_timestamp (most recent in buffer)
def test_decay_event_last_outcome_timestamp():
    outcomes = [
        _make_outcome(days_ago=10),
        _make_outcome(days_ago=5),
        _make_outcome(days_ago=20),
    ]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    p = events[0]["payload"]
    assert p["last_outcome_timestamp"] is not None
    # The most recent should be ~5 days ago
    ts = datetime.fromisoformat(p["last_outcome_timestamp"])
    expected = datetime.now(timezone.utc) - timedelta(days=5)
    assert abs((ts - expected).total_seconds()) < 120  # within 2 min tolerance


# 16. Decay event last_outcome_timestamp is None when buffer is empty
def test_decay_event_last_outcome_none_when_empty():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 1})
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert events[0]["payload"]["last_outcome_timestamp"] is None


# 17. Decay demotion resets counters
def test_decay_demotion_resets_counters():
    outcomes = [_make_outcome(days_ago=5) for _ in range(3)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    _run(ev.evaluate(audit))
    # After demotion, counters should be reset (empty outcomes)
    assert state.get_outcomes("cap.a") == []


# 18. Decay does NOT increment recent_failures
def test_decay_does_not_increment_recent_failures():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 2})
    # Pre-check: no failures
    failures_before = state.get_recent_failures("cap.a")
    assert len(failures_before) == 0
    _run(ev.evaluate(audit))
    failures_after = state.get_recent_failures("cap.a")
    assert len(failures_after) == 0


# 19. Multi-pass startup: WAL-2 idle 120 days -> WAL-1 then WAL-0
def test_multipass_wal2_idle_120_days():
    # Outcomes all at 120 days ago — outside both WAL-2 (60d) and WAL-1 (90d)
    outcomes = [_make_outcome(days_ago=120) for _ in range(5)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 2}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate_startup(audit))
    assert len(actions) == 2
    assert actions[0].from_level == 2
    assert actions[0].to_level == 1
    assert actions[1].from_level == 1
    assert actions[1].to_level == 0
    assert state.get_effective_wal("cap.a") == 0


# 20. Multi-pass startup: WAL-3 idle 120 days -> WAL-2 -> WAL-1 -> WAL-0
def test_multipass_wal3_idle_120_days():
    outcomes = [_make_outcome(days_ago=120) for _ in range(5)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 3}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate_startup(audit))
    assert len(actions) == 3
    assert actions[0].from_level == 3
    assert actions[0].to_level == 2
    assert actions[1].from_level == 2
    assert actions[1].to_level == 1
    assert actions[2].from_level == 1
    assert actions[2].to_level == 0
    assert state.get_effective_wal("cap.a") == 0


# 21. Multi-pass terminates when no demotions occur
def test_multipass_terminates_at_stable_state():
    # WAL-1 with sufficient outcomes — no demotion ever
    outcomes = [_make_outcome(days_ago=i) for i in range(15)]
    ev, state, audit = _setup(
        ["cap.a"], wal_levels={"cap.a": 1}, outcomes={"cap.a": outcomes}
    )
    actions = _run(ev.evaluate_startup(audit))
    assert actions == []
    assert state.get_effective_wal("cap.a") == 1


# 22. Per-capability override with stricter window_days is accepted
def test_override_stricter_window_accepted():
    overrides = {
        "cap.a": {"wal_1": {"window_days": 60, "min_outcomes": 10}}
    }
    ev, state, audit = _setup(
        ["cap.a"],
        wal_levels={"cap.a": 1},
        decay_overrides=overrides,
    )
    # Place outcomes only within 60 days -> should be fine
    # But since the override window is 60d (stricter), test it works
    # Put 10 outcomes all at 70 days ago — inside 90d default but outside 60d override
    for _ in range(10):
        state.record_outcome("cap.a", _make_outcome(days_ago=70))

    actions = _run(ev.evaluate(audit))
    # Override window is 60 days, outcomes at 70 days are outside -> demotion
    assert len(actions) == 1
    assert actions[0].window_days == 60


# 23. Per-capability override with stricter min_outcomes is accepted
def test_override_stricter_min_outcomes_accepted():
    overrides = {
        "cap.a": {"wal_1": {"window_days": 90, "min_outcomes": 20}}
    }
    outcomes = [_make_outcome(days_ago=i) for i in range(15)]
    ev, state, audit = _setup(
        ["cap.a"],
        wal_levels={"cap.a": 1},
        outcomes={"cap.a": outcomes},
        decay_overrides=overrides,
    )
    actions = _run(ev.evaluate(audit))
    # Default requires 10 (would pass), but override requires 20 (15 < 20 -> demotion)
    assert len(actions) == 1
    assert actions[0].min_required == 20


# 24. Per-capability override with MORE PERMISSIVE window_days is REJECTED
def test_override_permissive_window_rejected():
    config_data = {
        "decay_policy": {
            "wal_1": {"window_days": 90, "min_outcomes": 10},
        },
        "decay_overrides": {
            "cap.a": {"wal_1": {"window_days": 120, "min_outcomes": 10}},
        },
    }
    try:
        load_decay_config(config_data)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "exceeds system default" in str(e)


# 25. Per-capability override with MORE PERMISSIVE min_outcomes is REJECTED
def test_override_permissive_min_outcomes_rejected():
    config_data = {
        "decay_policy": {
            "wal_1": {"window_days": 90, "min_outcomes": 10},
        },
        "decay_overrides": {
            "cap.a": {"wal_1": {"window_days": 90, "min_outcomes": 5}},
        },
    }
    try:
        load_decay_config(config_data)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "less than system default" in str(e)


# 26. Missing decay_policy key disables decay entirely
def test_missing_decay_policy_disables_decay():
    config = load_decay_config({})
    assert config.enabled is False


# 27. Disabled decay logged at startup (evaluator returns empty)
def test_disabled_decay_returns_empty():
    config = load_decay_config({})
    tmpdir = tempfile.mkdtemp()
    state_path = os.path.join(tmpdir, "state.json")
    config_path = os.path.join(tmpdir, "caps.json")
    cap_data = _make_cap_config("cap.a")
    _write_json(config_path, cap_data)
    registry = CapabilityRegistry(config_path)
    registry.load()
    state = CapabilityStateManager(state_path)
    state.initialize_from_registry(registry)
    state.set_effective_wal("cap.a", 2)

    evaluator = DecayEvaluator(config, state)
    audit = MockAuditClient()
    actions = _run(evaluator.evaluate(audit))
    assert actions == []
    # WAL should remain unchanged
    assert state.get_effective_wal("cap.a") == 2


# 28. window_days=0 rejected at validation
def test_window_days_zero_rejected():
    config_data = {
        "decay_policy": {
            "wal_1": {"window_days": 0, "min_outcomes": 10},
        },
    }
    try:
        load_decay_config(config_data)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "window_days must be > 0" in str(e)


# 29. min_outcomes=0 rejected at validation
def test_min_outcomes_zero_rejected():
    config_data = {
        "decay_policy": {
            "wal_1": {"window_days": 90, "min_outcomes": 0},
        },
    }
    try:
        load_decay_config(config_data)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "min_outcomes must be > 0" in str(e)


# 30. Multiple capabilities evaluated independently in same cycle
def test_multiple_capabilities_independent():
    outcomes_a = [_make_outcome(days_ago=i) for i in range(5)]  # 5 < 10 for WAL-1
    outcomes_b = [_make_outcome(days_ago=i) for i in range(15)]  # 15 >= 10 for WAL-1
    ev, state, audit = _setup(
        ["cap.a", "cap.b"],
        wal_levels={"cap.a": 1, "cap.b": 1},
        outcomes={"cap.a": outcomes_a, "cap.b": outcomes_b},
    )
    actions = _run(ev.evaluate(audit))
    assert len(actions) == 1
    assert actions[0].capability_id == "cap.a"
    assert state.get_effective_wal("cap.a") == 0
    assert state.get_effective_wal("cap.b") == 1


# ===========================================================================
# Additional tests beyond the required 30
# ===========================================================================


# 31. Negative window_days rejected at validation
def test_negative_window_days_rejected():
    config_data = {
        "decay_policy": {
            "wal_1": {"window_days": -5, "min_outcomes": 10},
        },
    }
    try:
        load_decay_config(config_data)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "window_days must be > 0" in str(e)


# 32. Decay event has correct from_level and to_level in payload
def test_decay_event_from_to_levels():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 2})
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["from_level"] == 2
    assert p["to_level"] == 1


# 33. Decay event has correct capability_id on envelope
def test_decay_event_capability_id_on_envelope():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 1})
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert events[0]["capability_id"] == "cap.a"


# 34. Decay event wal_level on envelope is the to_level
def test_decay_event_wal_level_envelope():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 2})
    _run(ev.evaluate(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert events[0]["wal_level"] == 1  # to_level = from_level - 1


# 35. Multi-pass emits one event per demotion step
def test_multipass_emits_event_per_step():
    ev, state, audit = _setup(["cap.a"], wal_levels={"cap.a": 3})
    _run(ev.evaluate_startup(audit))
    events = audit.get_events_by_type("wal.demoted")
    assert len(events) == 3
    assert events[0]["payload"]["from_level"] == 3
    assert events[1]["payload"]["from_level"] == 2
    assert events[2]["payload"]["from_level"] == 1
