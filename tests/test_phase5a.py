"""Phase 5A unit tests — override types, sentinel check, event builders, Job extensions."""

from __future__ import annotations

import sys
import os
import re

# Add orchestrator to path so we can import modules directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from override_types import (
    OverrideType,
    VALID_OVERRIDE_TARGETS,
    SENTINEL_FAILURE_IDS,
    is_sentinel_failure,
    SPAWNS_SUCCESSOR,
    CONDITIONAL_DEMOTION_TYPES,
    TERMINAL_STATES,
)
from events import (
    event_human_override,
    event_job_revoked,
    event_job_failed,
)
from models import Job, JobStatus


# UUIDv7 pattern from event_validator
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _assert_valid_envelope(evt: dict, *, event_type: str, source: str, durability: str = "durable") -> None:
    """Validate an event envelope against the schema rules from event_validator.py."""
    assert evt["schema_version"] == "2.0"
    assert UUID_V7_RE.match(evt["source_event_id"]), f"Bad source_event_id: {evt['source_event_id']}"
    assert isinstance(evt["timestamp"], str) and len(evt["timestamp"]) > 0
    assert evt["event_type"] == event_type
    assert evt["source"] == source
    assert evt["durability"] == durability
    assert isinstance(evt["payload"], dict)
    # Nullable fields must be present
    for key in ("job_id", "parent_job_id", "capability_id", "wal_level"):
        assert key in evt, f"Missing nullable field: {key}"


# ---------- a. OverrideType enum ----------

class TestOverrideType:
    def test_all_four_values(self):
        assert OverrideType.cancel == "cancel"
        assert OverrideType.redirect == "redirect"
        assert OverrideType.modify == "modify"
        assert OverrideType.escalate == "escalate"

    def test_string_values_match(self):
        for member in OverrideType:
            assert member.value == member.name

    def test_member_count(self):
        assert len(OverrideType) == 4


# ---------- b. is_sentinel_failure ----------

class TestIsSentinelFailure:
    def test_true_for_each_sentinel(self):
        for sid in SENTINEL_FAILURE_IDS:
            assert is_sentinel_failure(sid) is True

    def test_false_for_real_capability(self):
        assert is_sentinel_failure("route.cloud.claude") is False

    def test_false_for_none(self):
        assert is_sentinel_failure(None) is False


# ---------- c. SPAWNS_SUCCESSOR ----------

class TestSpawnsSuccessor:
    def test_redirect_and_escalate_in(self):
        assert OverrideType.redirect in SPAWNS_SUCCESSOR
        assert OverrideType.escalate in SPAWNS_SUCCESSOR

    def test_cancel_and_modify_not_in(self):
        assert OverrideType.cancel not in SPAWNS_SUCCESSOR
        assert OverrideType.modify not in SPAWNS_SUCCESSOR


# ---------- d. CONDITIONAL_DEMOTION_TYPES ----------

class TestConditionalDemotionTypes:
    def test_cancel_and_redirect_in(self):
        assert OverrideType.cancel in CONDITIONAL_DEMOTION_TYPES
        assert OverrideType.redirect in CONDITIONAL_DEMOTION_TYPES

    def test_modify_and_escalate_not_in(self):
        assert OverrideType.modify not in CONDITIONAL_DEMOTION_TYPES
        assert OverrideType.escalate not in CONDITIONAL_DEMOTION_TYPES


# ---------- e. event_human_override ----------

class TestEventHumanOverride:
    def test_envelope_fields(self):
        evt = event_human_override(
            job_id="019078a0-0000-7000-8000-000000000001",
            override_type="cancel",
            target="routing",
            detail="user cancelled",
            device="watch",
        )
        _assert_valid_envelope(evt, event_type="human.override", source="human")

    def test_payload_fields(self):
        evt = event_human_override(
            job_id="019078a0-0000-7000-8000-000000000001",
            override_type="redirect",
            target="response",
            detail="route.cloud.openai",
            device="phone",
        )
        p = evt["payload"]
        assert p["job_id"] == "019078a0-0000-7000-8000-000000000001"
        assert p["override_type"] == "redirect"
        assert p["target"] == "response"
        assert p["detail"] == "route.cloud.openai"
        assert p["device"] == "phone"


# ---------- f. event_job_revoked ----------

class TestEventJobRevoked:
    def test_envelope_fields(self):
        evt = event_job_revoked(
            job_id="019078a0-0000-7000-8000-000000000001",
            reason="user_cancel",
            override_source_event_id="019078a0-0000-7000-8000-000000000099",
        )
        _assert_valid_envelope(evt, event_type="job.revoked", source="orchestrator")

    def test_payload_fields(self):
        evt = event_job_revoked(
            job_id="019078a0-0000-7000-8000-000000000001",
            reason="escalation_supersede",
            override_source_event_id="019078a0-0000-7000-8000-aaaaaaaaaaaa",
            successor_job_id="019078a0-0000-7000-8000-bbbbbbbbbbbb",
            memory_objects_superseded=["mem-1", "mem-2"],
        )
        p = evt["payload"]
        assert p["reason"] == "escalation_supersede"
        assert p["override_source_event_id"] == "019078a0-0000-7000-8000-aaaaaaaaaaaa"
        assert p["successor_job_id"] == "019078a0-0000-7000-8000-bbbbbbbbbbbb"
        assert p["memory_objects_superseded"] == ["mem-1", "mem-2"]

    def test_memory_objects_defaults_empty(self):
        evt = event_job_revoked(
            job_id="019078a0-0000-7000-8000-000000000001",
            reason="user_cancel",
            override_source_event_id="019078a0-0000-7000-8000-000000000099",
        )
        assert evt["payload"]["memory_objects_superseded"] == []

    def test_successor_job_id_nullable(self):
        evt = event_job_revoked(
            job_id="019078a0-0000-7000-8000-000000000001",
            reason="user_cancel",
            override_source_event_id="019078a0-0000-7000-8000-000000000099",
        )
        assert evt["payload"]["successor_job_id"] is None


# ---------- g. event_job_failed with failing_capability_id ----------

class TestEventJobFailedCapabilityId:
    def test_with_failing_capability_id(self):
        evt = event_job_failed(
            job_id="019078a0-0000-7000-8000-000000000001",
            error_class="override_cancel",
            detail="user cancelled job",
            failing_capability_id="egress_config",
        )
        assert evt["payload"]["failing_capability_id"] == "egress_config"

    def test_without_failing_capability_id_defaults_none(self):
        evt = event_job_failed(
            job_id="019078a0-0000-7000-8000-000000000001",
            error_class="pipeline_error",
            detail="something broke",
        )
        assert evt["payload"]["failing_capability_id"] is None


# ---------- h. Job dataclass extensions ----------

class TestJobDataclassExtensions:
    def test_new_fields_default_none(self):
        job = Job(
            job_id="j1",
            raw_input="test",
            input_modality="text",
            device="watch",
        )
        assert job.parent_job_id is None
        assert job.revoked_at is None
        assert job.override_type is None

    def test_new_fields_set_explicitly(self):
        job = Job(
            job_id="j2",
            raw_input="test",
            input_modality="text",
            device="phone",
            parent_job_id="j1",
            revoked_at="2026-04-03T00:00:00.000000Z",
            override_type="cancel",
        )
        assert job.parent_job_id == "j1"
        assert job.revoked_at == "2026-04-03T00:00:00.000000Z"
        assert job.override_type == "cancel"


# ---------- i. TERMINAL_STATES ----------

class TestTerminalStates:
    def test_delivered_present(self):
        assert "delivered" in TERMINAL_STATES

    def test_failed_present(self):
        assert "failed" in TERMINAL_STATES

    def test_revoked_present(self):
        assert "revoked" in TERMINAL_STATES

    def test_count(self):
        assert len(TERMINAL_STATES) == 3


# ---------- Extra: JobStatus.revoked exists ----------

class TestJobStatusRevoked:
    def test_revoked_in_enum(self):
        assert JobStatus.revoked.value == "revoked"
