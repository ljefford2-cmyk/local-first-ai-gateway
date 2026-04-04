"""Pydantic models for the DRNT orchestrator HTTP API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------- Enums ----------

class InputModality(str, Enum):
    voice = "voice"
    text = "text"
    tap = "tap"


class Device(str, Enum):
    watch = "watch"
    phone = "phone"


class JobStatus(str, Enum):
    submitted = "submitted"
    classified = "classified"
    dispatched = "dispatched"
    response_received = "response_received"
    delivered = "delivered"
    failed = "failed"
    revoked = "revoked"


# ---------- HTTP request / response ----------

class JobSubmitRequest(BaseModel):
    raw_input: str
    input_modality: InputModality
    device: Device
    idempotency_key: Optional[str] = None


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    created_at: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    classified_at: Optional[str] = None
    dispatched_at: Optional[str] = None
    response_received_at: Optional[str] = None
    delivered_at: Optional[str] = None
    request_category: Optional[str] = None
    routing_recommendation: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None


class HealthResponse(BaseModel):
    orchestrator_status: str
    audit_log_status: str
    ollama_status: str
    last_successful_cloud_probe_timestamp: Optional[str] = None
    hub_suspended: bool = False


# ---------- Internal job record ----------

@dataclass
class Job:
    job_id: str
    raw_input: str
    input_modality: str
    device: str
    status: str = JobStatus.submitted.value
    created_at: str = ""
    classified_at: Optional[str] = None
    dispatched_at: Optional[str] = None
    response_received_at: Optional[str] = None
    delivered_at: Optional[str] = None
    request_category: Optional[str] = None
    routing_recommendation: Optional[str] = None
    candidate_models: Optional[list[str]] = None
    governing_capability_id: Optional[str] = None
    result: Optional[str] = None
    result_id: Optional[str] = None
    error: Optional[str] = None
    # Phase 5A: override-related fields
    parent_job_id: Optional[str] = None
    revoked_at: Optional[str] = None
    override_type: Optional[str] = None
    # Phase 5C: last failing capability for sentinel demotion check
    last_failing_capability_id: Optional[str] = None
    # Phase 5E: WAL level at classification time (for auto-accept window)
    wal_level: Optional[int] = None
    # Phase 7B: idempotency key for dedup / stale recovery re-dispatch
    idempotency_key: Optional[str] = None
    # Phase 7D: recovery re-dispatch counter (max 2 re-dispatches)
    recovery_dispatch_count: int = 0
