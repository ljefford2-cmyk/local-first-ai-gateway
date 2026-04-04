"""Phase 7A unit tests — Event builders + enhanced health endpoint.

Covers: system.connectivity (hub_cloud, device), system.hub_switch,
wal.demoted (temporal_decay), job.queued, and orchestrator /health.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from events import (
    event_job_failed,
    event_job_queued,
    event_system_connectivity_device,
    event_system_connectivity_hub_cloud,
    event_system_hub_switch,
    event_wal_demoted_temporal_decay,
)

# UUIDv7 pattern: version nibble is 7, variant bits are 8/9/a/b
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------- 1. event_system_connectivity_hub_cloud envelope ----------

def test_hub_cloud_envelope():
    evt = event_system_connectivity_hub_cloud(
        state="down", affected_routes=["r1"], queued_jobs=3,
    )
    assert evt["event_type"] == "system.connectivity"
    assert evt["source"] == "orchestrator"
    assert evt["durability"] == "durable"
    assert evt["job_id"] is None


# ---------- 2. hub-cloud payload fields ----------

def test_hub_cloud_payload_fields():
    evt = event_system_connectivity_hub_cloud(
        state="up", affected_routes=["r1", "r2"], queued_jobs=0,
    )
    p = evt["payload"]
    assert p["link"] == "hub_cloud"
    assert p["state"] == "up"
    assert p["affected_routes"] == ["r1", "r2"]
    assert p["queued_jobs"] == 0


# ---------- 3. hub-cloud state enum values ----------

def test_hub_cloud_state_down():
    evt = event_system_connectivity_hub_cloud(
        state="down", affected_routes=[], queued_jobs=0,
    )
    assert evt["payload"]["state"] == "down"


def test_hub_cloud_state_up():
    evt = event_system_connectivity_hub_cloud(
        state="up", affected_routes=[], queued_jobs=0,
    )
    assert evt["payload"]["state"] == "up"


def test_hub_cloud_state_degraded():
    evt = event_system_connectivity_hub_cloud(
        state="degraded", affected_routes=["r1"], queued_jobs=1,
    )
    assert evt["payload"]["state"] == "degraded"


# ---------- 4. event_system_connectivity_device envelope ----------

def test_device_connectivity_envelope():
    evt = event_system_connectivity_device(
        link="watch_phone", state="down", queued_requests=5,
    )
    assert evt["event_type"] == "system.connectivity"
    assert evt["source"] == "phone_app"
    assert evt["durability"] == "durable"
    assert evt["job_id"] is None


# ---------- 5. device connectivity payload fields ----------

def test_device_connectivity_payload_fields():
    evt = event_system_connectivity_device(
        link="phone_hub",
        state="up",
        queued_requests=2,
        duration_ms=12000,
        replayed_requests=2,
        dropped_requests=1,
        replay_conflicts=0,
    )
    p = evt["payload"]
    assert p["link"] == "phone_hub"
    assert p["state"] == "up"
    assert p["duration_ms"] == 12000
    assert p["queued_requests"] == 2
    assert p["replayed_requests"] == 2
    assert p["dropped_requests"] == 1
    assert p["replay_conflicts"] == 0


# ---------- 6. device connectivity link enum values ----------

def test_device_link_watch_phone():
    evt = event_system_connectivity_device(
        link="watch_phone", state="down", queued_requests=0,
    )
    assert evt["payload"]["link"] == "watch_phone"


def test_device_link_phone_hub():
    evt = event_system_connectivity_device(
        link="phone_hub", state="up", queued_requests=0, duration_ms=500,
    )
    assert evt["payload"]["link"] == "phone_hub"


# ---------- 7. device connectivity duration_ms semantics ----------

def test_device_duration_none_on_down():
    evt = event_system_connectivity_device(
        link="watch_phone", state="down", queued_requests=3,
    )
    assert evt["payload"]["duration_ms"] is None


def test_device_duration_populated_on_up():
    evt = event_system_connectivity_device(
        link="watch_phone", state="up", queued_requests=0, duration_ms=9500,
    )
    assert evt["payload"]["duration_ms"] == 9500


# ---------- 8. event_system_hub_switch envelope ----------

def test_hub_switch_envelope():
    evt = event_system_hub_switch(
        from_hub="hub-a.local",
        to_hub="hub-b.local",
        reason="manual",
        pending_jobs_on_old_hub=0,
        suspend_command_delivered=True,
    )
    assert evt["event_type"] == "system.hub_switch"
    assert evt["source"] == "phone_app"
    assert evt["durability"] == "durable"
    assert evt["job_id"] is None


# ---------- 9. hub switch payload fields ----------

def test_hub_switch_payload_fields():
    evt = event_system_hub_switch(
        from_hub="hub-a.local",
        to_hub="hub-b.local",
        reason="primary_failure",
        pending_jobs_on_old_hub=4,
        suspend_command_delivered=False,
    )
    p = evt["payload"]
    assert p["from_hub"] == "hub-a.local"
    assert p["to_hub"] == "hub-b.local"
    assert p["reason"] == "primary_failure"
    assert p["pending_jobs_on_old_hub"] == 4
    assert p["suspend_command_delivered"] is False


# ---------- 10. hub switch reason enum ----------

def test_hub_switch_reason_manual():
    evt = event_system_hub_switch(
        from_hub="a", to_hub="b", reason="manual",
        pending_jobs_on_old_hub=0, suspend_command_delivered=True,
    )
    assert evt["payload"]["reason"] == "manual"


def test_hub_switch_reason_primary_failure():
    evt = event_system_hub_switch(
        from_hub="a", to_hub="b", reason="primary_failure",
        pending_jobs_on_old_hub=2, suspend_command_delivered=False,
    )
    assert evt["payload"]["reason"] == "primary_failure"


# ---------- 11. event_wal_demoted_temporal_decay envelope ----------

def test_temporal_decay_envelope():
    evt = event_wal_demoted_temporal_decay(
        capability_id="cap.test",
        from_level=3,
        window_days=30,
        outcomes_in_window=1,
        min_required=5,
    )
    assert evt["event_type"] == "wal.demoted"
    assert evt["source"] == "orchestrator"
    assert evt["durability"] == "durable"
    assert evt["job_id"] is None
    assert evt["capability_id"] == "cap.test"
    assert evt["wal_level"] == 2  # from_level - 1


# ---------- 12. temporal decay trigger ----------

def test_temporal_decay_trigger():
    evt = event_wal_demoted_temporal_decay(
        capability_id="cap.x", from_level=2,
        window_days=14, outcomes_in_window=0, min_required=3,
    )
    assert evt["payload"]["trigger"] == "temporal_decay"


# ---------- 13. temporal decay to_level is from_level - 1 ----------

def test_temporal_decay_to_level():
    for level in (1, 2, 3, 5):
        evt = event_wal_demoted_temporal_decay(
            capability_id="cap.x", from_level=level,
            window_days=30, outcomes_in_window=0, min_required=3,
        )
        assert evt["payload"]["to_level"] == level - 1
        assert evt["wal_level"] == level - 1


# ---------- 14. temporal decay payload detail fields ----------

def test_temporal_decay_payload_detail():
    evt = event_wal_demoted_temporal_decay(
        capability_id="cap.y",
        from_level=4,
        window_days=60,
        outcomes_in_window=2,
        min_required=10,
        last_outcome_timestamp="2026-03-01T00:00:00.000000Z",
    )
    p = evt["payload"]
    assert p["capability_id"] == "cap.y"
    assert p["from_level"] == 4
    assert p["to_level"] == 3
    assert p["window_days"] == 60
    assert p["outcomes_in_window"] == 2
    assert p["min_required"] == 10
    assert p["last_outcome_timestamp"] == "2026-03-01T00:00:00.000000Z"


# ---------- 15. temporal decay last_outcome_timestamp accepts None ----------

def test_temporal_decay_last_outcome_none():
    evt = event_wal_demoted_temporal_decay(
        capability_id="cap.z", from_level=1,
        window_days=7, outcomes_in_window=0, min_required=2,
        last_outcome_timestamp=None,
    )
    assert evt["payload"]["last_outcome_timestamp"] is None


# ---------- 16. event_job_queued envelope ----------

def test_job_queued_envelope():
    evt = event_job_queued(
        job_id="job-1", reason="connectivity", position=1,
    )
    assert evt["event_type"] == "job.queued"
    assert evt["durability"] == "best_effort"
    assert evt["job_id"] == "job-1"


# ---------- 17. job queued payload fields ----------

def test_job_queued_payload_fields():
    evt = event_job_queued(
        job_id="job-2", reason="rate_limit", position=3, estimated_wait_ms=5000,
    )
    p = evt["payload"]
    assert p["reason"] == "rate_limit"
    assert p["position"] == 3
    assert p["estimated_wait_ms"] == 5000


# ---------- 18. job queued reason accepts "recovery" ----------

def test_job_queued_reason_recovery():
    evt = event_job_queued(
        job_id="job-3", reason="recovery", position=0,
    )
    assert evt["payload"]["reason"] == "recovery"


# ---------- 19. all new events use schema_version "2.0" ----------

def test_all_new_events_schema_version():
    builders = [
        event_system_connectivity_hub_cloud(state="up", affected_routes=[], queued_jobs=0),
        event_system_connectivity_device(link="watch_phone", state="down", queued_requests=0),
        event_system_hub_switch(
            from_hub="a", to_hub="b", reason="manual",
            pending_jobs_on_old_hub=0, suspend_command_delivered=True,
        ),
        event_wal_demoted_temporal_decay(
            capability_id="c", from_level=2,
            window_days=7, outcomes_in_window=0, min_required=1,
        ),
        event_job_queued(job_id="j", reason="capacity", position=0),
    ]
    for evt in builders:
        assert evt["schema_version"] == "2.0", f"{evt['event_type']} missing schema_version 2.0"


# ---------- 20. all new events generate unique UUIDv7 source_event_id ----------

def test_all_new_events_unique_uuidv7():
    events = [
        event_system_connectivity_hub_cloud(state="down", affected_routes=[], queued_jobs=0),
        event_system_connectivity_hub_cloud(state="up", affected_routes=[], queued_jobs=0),
        event_system_connectivity_device(link="watch_phone", state="down", queued_requests=0),
        event_system_hub_switch(
            from_hub="a", to_hub="b", reason="manual",
            pending_jobs_on_old_hub=0, suspend_command_delivered=True,
        ),
        event_wal_demoted_temporal_decay(
            capability_id="c", from_level=2,
            window_days=7, outcomes_in_window=0, min_required=1,
        ),
        event_job_queued(job_id="j", reason="capacity", position=0),
    ]
    ids = set()
    for evt in events:
        sid = evt["source_event_id"]
        assert UUID_V7_RE.match(sid), f"Not a UUIDv7: {sid}"
        assert sid not in ids, f"Duplicate source_event_id: {sid}"
        ids.add(sid)


# ---------- 21. event_job_failed accepts recovery_exhausted ----------

def test_job_failed_recovery_exhausted():
    evt = event_job_failed(
        job_id="job-fail-1",
        error_class="recovery_exhausted",
        detail="All recovery strategies exhausted",
        failing_capability_id="cap.test",
    )
    assert evt["payload"]["error_class"] == "recovery_exhausted"
    assert evt["event_type"] == "job.failed"


# ---------- 22. job queued estimated_wait_ms defaults to None ----------

def test_job_queued_estimated_wait_none():
    evt = event_job_queued(job_id="j", reason="connectivity", position=1)
    assert evt["payload"]["estimated_wait_ms"] is None


# ---------- 23. hub-cloud affected_routes is a list ----------

def test_hub_cloud_affected_routes_list():
    evt = event_system_connectivity_hub_cloud(
        state="degraded",
        affected_routes=["route.cloud.openai", "route.cloud.anthropic"],
        queued_jobs=5,
    )
    assert isinstance(evt["payload"]["affected_routes"], list)
    assert len(evt["payload"]["affected_routes"]) == 2


# ---------- 24. all new events have timestamp ----------

def test_all_new_events_have_timestamp():
    evt = event_system_connectivity_hub_cloud(
        state="up", affected_routes=[], queued_jobs=0,
    )
    assert "timestamp" in evt
    assert evt["timestamp"].endswith("Z")
