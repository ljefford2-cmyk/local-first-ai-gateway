"""Event dataclass and type definitions for the DRNT Audit Log Writer."""

from dataclasses import dataclass, field
from typing import Optional


VALID_SOURCES = frozenset({
    "orchestrator",
    "context_packager",
    "egress_gateway",
    "watch_app",
    "phone_app",
    "human",
    "system",
    "log_writer",
})

VALID_DURABILITY = frozenset({"durable", "best_effort"})

SCHEMA_VERSION = "2.0"


@dataclass
class InboundEvent:
    """Event as received from an emitter (before writer-owned fields are assigned)."""

    schema_version: str
    source_event_id: str
    timestamp: str
    event_type: str
    job_id: Optional[str]
    parent_job_id: Optional[str]
    capability_id: Optional[str]
    wal_level: Optional[int]
    source: str
    durability: str
    payload: dict


@dataclass
class CommittedEvent:
    """Fully committed event with all writer-assigned fields."""

    schema_version: str
    event_id: str
    source_event_id: str
    timestamp: str
    committed_at: str
    event_type: str
    job_id: Optional[str]
    parent_job_id: Optional[str]
    capability_id: Optional[str]
    wal_level: Optional[int]
    source: str
    prev_hash: str
    durability: str
    payload: dict

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "source_event_id": self.source_event_id,
            "timestamp": self.timestamp,
            "committed_at": self.committed_at,
            "event_type": self.event_type,
            "job_id": self.job_id,
            "parent_job_id": self.parent_job_id,
            "capability_id": self.capability_id,
            "wal_level": self.wal_level,
            "source": self.source,
            "prev_hash": self.prev_hash,
            "durability": self.durability,
            "payload": self.payload,
        }


@dataclass
class AckResponse:
    """Successful write acknowledgement."""

    status: str = "ok"
    event_id: str = ""
    committed_at: str = ""
    prev_hash: str = ""
    sequence: int = 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "event_id": self.event_id,
            "committed_at": self.committed_at,
            "prev_hash": self.prev_hash,
            "sequence": self.sequence,
        }


@dataclass
class ErrorResponse:
    """Validation error response."""

    status: str = "error"
    message: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "message": self.message}


@dataclass
class DuplicateResponse:
    """Duplicate event response."""

    status: str = "duplicate"
    source_event_id: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "source_event_id": self.source_event_id}
