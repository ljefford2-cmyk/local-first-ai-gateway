"""Phase 4A backend contract — schema-only tests.

Covers the Phase 4A.1 schema and enum changes:
- JobStatus.proposal_ready and JobStatus.closed_no_action members
- Proposal response model
- JobSubmitRequest extension with client_source, client_source_event_id,
  client_timestamp; extra="forbid"
- ReviewRequest model with extra="forbid"
- JobStatusResponse.proposal optional field

Endpoint wiring, job_manager behavior, and persistence are out of scope
for Phase 4A.1 and are not exercised here.
"""

from __future__ import annotations

import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from models import (
    ClientSource,
    JobStatus,
    JobStatusResponse,
    JobSubmitRequest,
    Proposal,
    ReviewRequest,
)


# ---------- JobStatus enum members ----------

def test_jobstatus_proposal_ready_exists_and_serializes():
    assert JobStatus.proposal_ready.value == "proposal_ready"


def test_jobstatus_closed_no_action_exists_and_serializes():
    assert JobStatus.closed_no_action.value == "closed_no_action"


def test_jobstatus_preserves_existing_members():
    expected = {
        "submitted",
        "classified",
        "dispatched",
        "response_received",
        "delivered",
        "failed",
        "revoked",
        "proposal_ready",
        "closed_no_action",
    }
    assert {m.value for m in JobStatus} == expected


# ---------- Proposal model ----------

def _valid_proposal_payload() -> dict:
    return {
        "proposal_id": "prop-001",
        "job_id": "job-001",
        "result_id": "res-001",
        "response_hash": "sha256:abc",
        "proposed_by": "model:claude-sonnet-4",
        "governing_capability_id": "route.cloud.claude",
        "confidence": 0.87,
        "auto_accept_at": "2026-04-26T12:00:00Z",
    }


def test_proposal_accepts_valid_payload():
    p = Proposal(**_valid_proposal_payload())
    assert p.proposal_id == "prop-001"
    assert p.auto_accept_at == "2026-04-26T12:00:00Z"


def test_proposal_auto_accept_at_defaults_to_none():
    payload = _valid_proposal_payload()
    payload.pop("auto_accept_at")
    p = Proposal(**payload)
    assert p.auto_accept_at is None


def test_proposal_rejects_missing_required_field():
    payload = _valid_proposal_payload()
    payload.pop("proposal_id")
    with pytest.raises(ValidationError):
        Proposal(**payload)


# ---------- JobSubmitRequest extension ----------

def _minimal_submit_payload() -> dict:
    return {
        "raw_input": "What is the capital of France?",
        "input_modality": "text",
        "device": "phone",
    }


def test_jobsubmitrequest_backward_compat_without_client_fields():
    """AC #7: existing payloads without client_* fields must still validate."""
    req = JobSubmitRequest(**_minimal_submit_payload())
    assert req.client_source is None
    assert req.client_source_event_id is None
    assert req.client_timestamp is None


def test_jobsubmitrequest_accepts_client_fields():
    payload = _minimal_submit_payload()
    payload.update(
        {
            "client_source": "phone_app",
            "client_source_event_id": "01941b8e-7c3f-7000-8000-000000000001",
            "client_timestamp": "2026-04-26T11:30:00Z",
        }
    )
    req = JobSubmitRequest(**payload)
    assert req.client_source == ClientSource.phone_app
    assert req.client_source_event_id == "01941b8e-7c3f-7000-8000-000000000001"
    assert req.client_timestamp == "2026-04-26T11:30:00Z"


def test_jobsubmitrequest_accepts_watch_app_client_source():
    payload = _minimal_submit_payload()
    payload["client_source"] = "watch_app"
    req = JobSubmitRequest(**payload)
    assert req.client_source == ClientSource.watch_app


def test_jobsubmitrequest_rejects_unknown_field():
    payload = _minimal_submit_payload()
    payload["bogus_field"] = "value"
    with pytest.raises(ValidationError):
        JobSubmitRequest(**payload)


def test_jobsubmitrequest_rejects_invalid_client_source():
    payload = _minimal_submit_payload()
    payload["client_source"] = "desktop_app"
    with pytest.raises(ValidationError):
        JobSubmitRequest(**payload)


# ---------- ReviewRequest model ----------

def _valid_review_payload() -> dict:
    return {
        "decision": "approve",
        "result_id": "res-001",
        "response_hash": "sha256:abc",
        "decision_idempotency_key": "rev-key-001",
    }


def test_reviewrequest_accepts_valid_payload():
    r = ReviewRequest(**_valid_review_payload())
    assert r.decision == "approve"
    assert r.result_id == "res-001"
    assert r.response_hash == "sha256:abc"
    assert r.decision_idempotency_key == "rev-key-001"


@pytest.mark.parametrize(
    "missing_field",
    ["decision", "result_id", "response_hash", "decision_idempotency_key"],
)
def test_reviewrequest_rejects_missing_required_field(missing_field: str):
    payload = _valid_review_payload()
    payload.pop(missing_field)
    with pytest.raises(ValidationError):
        ReviewRequest(**payload)


def test_reviewrequest_rejects_unknown_field():
    payload = _valid_review_payload()
    payload["bogus_field"] = "value"
    with pytest.raises(ValidationError):
        ReviewRequest(**payload)


# ---------- JobStatusResponse.proposal field ----------

def _minimal_status_response_payload() -> dict:
    return {
        "job_id": "job-001",
        "status": "submitted",
        "created_at": "2026-04-26T11:00:00Z",
    }


def test_jobstatusresponse_proposal_defaults_to_none():
    resp = JobStatusResponse(**_minimal_status_response_payload())
    assert resp.proposal is None


def test_jobstatusresponse_accepts_proposal_object():
    payload = _minimal_status_response_payload()
    payload["status"] = "proposal_ready"
    payload["proposal"] = _valid_proposal_payload()
    resp = JobStatusResponse(**payload)
    assert resp.proposal is not None
    assert resp.proposal.proposal_id == "prop-001"
    assert resp.proposal.confidence == 0.87
