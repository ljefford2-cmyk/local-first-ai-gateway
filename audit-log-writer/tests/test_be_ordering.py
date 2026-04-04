"""Test that best-effort events are correctly ordered in the hash chain
when interleaved with durable events."""

from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timezone

import pytest
from uuid_utils import uuid7

# Add the audit-log-writer root so 'src' is importable as a package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.hash_chain import verify_chain, compute_hash, GENESIS_HASH
from src.log_writer import AuditLogWriter


def _make_event(event_type: str, durability: str, job_id: str | None = None) -> dict:
    return {
        "schema_version": "2.0",
        "source_event_id": str(uuid7()),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event_type": event_type,
        "job_id": job_id,
        "parent_job_id": None,
        "capability_id": None,
        "wal_level": None,
        "source": "orchestrator",
        "durability": durability,
        "payload": {"test": True},
    }


def _read_log_lines(log_dir: str) -> list[str]:
    """Read all non-empty lines from the single log file in the directory."""
    lines = []
    for fname in sorted(os.listdir(log_dir)):
        if fname.endswith(".jsonl"):
            with open(os.path.join(log_dir, fname), "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.rstrip("\n")
                    if stripped:
                        lines.append(stripped)
    return lines


class TestBestEffortOrdering:
    """Verify that BE events are flushed before durable events in the hash chain."""

    def test_interleaved_be_and_durable_ordering(self, tmp_path):
        """Send durable-1, BE-1, BE-2, durable-2, BE-3, durable-3.
        Verify all 6 events appear in correct order with a valid hash chain."""
        log_dir = str(tmp_path / "audit")
        os.makedirs(log_dir, exist_ok=True)

        writer = AuditLogWriter(
            socket_path=str(tmp_path / "audit.sock"),
            log_dir=log_dir,
        )
        # Initialize chain state without reading existing files
        writer.file_manager  # already created in __init__
        # Skip _initialize() which tries to recover from files; start fresh

        # Send events in sequence
        events = [
            ("durable-1", "durable"),
            ("BE-1", "best_effort"),
            ("BE-2", "best_effort"),
            ("durable-2", "durable"),
            ("BE-3", "best_effort"),
            ("durable-3", "durable"),
        ]

        for event_type, durability in events:
            data = _make_event(event_type, durability)
            resp = writer._handle_event(data)
            assert resp["status"] == "ok", f"Event {event_type} failed: {resp}"

        # Read the log file
        lines = _read_log_lines(log_dir)

        # All 6 events should be present
        assert len(lines) == 6, f"Expected 6 events, got {len(lines)}"

        # Parse event types in order
        written_types = [json.loads(line)["event_type"] for line in lines]

        # Verify ordering: BE-1 and BE-2 flushed before durable-2,
        # BE-3 flushed before durable-3
        assert written_types == [
            "durable-1",
            "BE-1",
            "BE-2",
            "durable-2",
            "BE-3",
            "durable-3",
        ]

        # Verify hash chain integrity
        valid, break_index, message = verify_chain(lines)
        assert valid, f"Hash chain broken at index {break_index}: {message}"

    def test_be_events_flushed_before_durable(self, tmp_path):
        """BE events buffered between two durable events appear before the second durable."""
        log_dir = str(tmp_path / "audit")
        os.makedirs(log_dir, exist_ok=True)

        writer = AuditLogWriter(
            socket_path=str(tmp_path / "audit.sock"),
            log_dir=log_dir,
        )

        # durable-1, then three BE events, then durable-2
        writer._handle_event(_make_event("d1", "durable"))
        writer._handle_event(_make_event("be1", "best_effort"))
        writer._handle_event(_make_event("be2", "best_effort"))
        writer._handle_event(_make_event("be3", "best_effort"))
        writer._handle_event(_make_event("d2", "durable"))

        lines = _read_log_lines(log_dir)
        types = [json.loads(line)["event_type"] for line in lines]

        # All BE events must appear between d1 and d2
        assert types == ["d1", "be1", "be2", "be3", "d2"]

        # Chain must be valid
        valid, break_index, message = verify_chain(lines)
        assert valid, f"Hash chain broken at index {break_index}: {message}"

    def test_flush_on_shutdown(self, tmp_path):
        """BE events without a subsequent durable event are written on explicit flush."""
        log_dir = str(tmp_path / "audit")
        os.makedirs(log_dir, exist_ok=True)

        writer = AuditLogWriter(
            socket_path=str(tmp_path / "audit.sock"),
            log_dir=log_dir,
        )

        # Send only BE events — no durable event to trigger flush
        writer._handle_event(_make_event("be-only-1", "best_effort"))
        writer._handle_event(_make_event("be-only-2", "best_effort"))

        # Before explicit flush, file should be empty or not exist
        lines_before = _read_log_lines(log_dir)
        assert len(lines_before) == 0, "BE events should be buffered, not on disk yet"

        # Explicit flush (as would happen on shutdown via _shutdown())
        writer._flush_best_effort()

        lines_after = _read_log_lines(log_dir)
        assert len(lines_after) == 2, f"Expected 2 events after flush, got {len(lines_after)}"

        types = [json.loads(line)["event_type"] for line in lines_after]
        assert types == ["be-only-1", "be-only-2"]

        # Chain must be valid
        valid, break_index, message = verify_chain(lines_after)
        assert valid, f"Hash chain broken at index {break_index}: {message}"

    def test_hash_chain_valid_across_all_durability_types(self, tmp_path):
        """Hash chain prev_hash is sequential regardless of durability."""
        log_dir = str(tmp_path / "audit")
        os.makedirs(log_dir, exist_ok=True)

        writer = AuditLogWriter(
            socket_path=str(tmp_path / "audit.sock"),
            log_dir=log_dir,
        )

        # Mixed pattern
        writer._handle_event(_make_event("d1", "durable"))
        writer._handle_event(_make_event("be1", "best_effort"))
        writer._handle_event(_make_event("d2", "durable"))
        writer._handle_event(_make_event("be2", "best_effort"))
        writer._handle_event(_make_event("be3", "best_effort"))
        writer._flush_best_effort()  # flush trailing BE events

        lines = _read_log_lines(log_dir)
        assert len(lines) == 5

        # Verify chain manually: each event's prev_hash == SHA-256 of previous JSON line
        first = json.loads(lines[0])
        assert first["prev_hash"] == GENESIS_HASH

        for i in range(1, len(lines)):
            expected = compute_hash(lines[i - 1])
            actual = json.loads(lines[i])["prev_hash"]
            assert actual == expected, (
                f"Event {i} ({json.loads(lines[i])['event_type']}): "
                f"expected prev_hash {expected[:16]}..., got {actual[:16]}..."
            )
