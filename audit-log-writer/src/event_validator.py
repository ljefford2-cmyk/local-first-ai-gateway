"""Envelope schema validation for inbound events."""

import re
from datetime import datetime, timezone

from .models import VALID_SOURCES, VALID_DURABILITY, SCHEMA_VERSION

# UUIDv7 regex: 8-4-7xxx-4-12 where version nibble is 7
UUID_V7_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

REQUIRED_FIELDS = [
    "schema_version",
    "source_event_id",
    "timestamp",
    "event_type",
    "source",
    "durability",
    "payload",
]

NULLABLE_FIELDS = ["job_id", "parent_job_id", "capability_id", "wal_level"]

# Fields that the writer assigns — must NOT be provided by emitter
WRITER_FIELDS = ["event_id", "committed_at", "prev_hash"]


def validate_event(data: dict) -> str | None:
    """Validate an inbound event dict against the envelope schema.

    Returns None if valid, or an error message string if invalid.
    """
    if not isinstance(data, dict):
        return "Event must be a JSON object"

    # Check all required fields are present
    for field_name in REQUIRED_FIELDS:
        if field_name not in data:
            return f"Missing required field: {field_name}"

    # Check nullable fields are present (they must be in the envelope, but can be null)
    for field_name in NULLABLE_FIELDS:
        if field_name not in data:
            return f"Missing required field: {field_name}"

    # schema_version
    if data["schema_version"] != SCHEMA_VERSION:
        return f"Invalid schema_version: expected '{SCHEMA_VERSION}', got '{data['schema_version']}'"

    # source_event_id — must be valid UUIDv7 format
    if not isinstance(data["source_event_id"], str) or not UUID_V7_PATTERN.match(data["source_event_id"]):
        return f"Invalid source_event_id: must be a valid UUIDv7 format"

    # timestamp — must be valid ISO 8601
    if not isinstance(data["timestamp"], str):
        return "Invalid timestamp: must be a string"
    try:
        datetime.fromisoformat(data["timestamp"])
    except (ValueError, TypeError):
        return f"Invalid timestamp: not a valid ISO 8601 format"

    # event_type — non-empty string
    if not isinstance(data["event_type"], str) or not data["event_type"]:
        return "Invalid event_type: must be a non-empty string"

    # source — enum
    if data["source"] not in VALID_SOURCES:
        return f"Invalid source: '{data['source']}'. Must be one of: {', '.join(sorted(VALID_SOURCES))}"

    # durability — enum
    if data["durability"] not in VALID_DURABILITY:
        return f"Invalid durability: '{data['durability']}'. Must be 'durable' or 'best_effort'"

    # payload — must be a JSON object
    if not isinstance(data["payload"], dict):
        return "Invalid payload: must be a JSON object"

    # Nullable field type checks
    if data["job_id"] is not None:
        if not isinstance(data["job_id"], str) or not UUID_V7_PATTERN.match(data["job_id"]):
            return "Invalid job_id: must be a valid UUIDv7 format or null"

    if data["parent_job_id"] is not None:
        if not isinstance(data["parent_job_id"], str) or not UUID_V7_PATTERN.match(data["parent_job_id"]):
            return "Invalid parent_job_id: must be a valid UUIDv7 format or null"

    if data["capability_id"] is not None:
        if not isinstance(data["capability_id"], str):
            return "Invalid capability_id: must be a string or null"

    if data["wal_level"] is not None:
        if not isinstance(data["wal_level"], int) or isinstance(data["wal_level"], bool):
            return "Invalid wal_level: must be an integer or null"
        if data["wal_level"] < -1 or data["wal_level"] > 3:
            return "Invalid wal_level: must be between -1 and 3"

    return None
