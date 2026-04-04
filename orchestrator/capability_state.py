"""Capability state manager: manages capabilities.state.json (runtime state with ring buffers and deques)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from capability_registry import CapabilityRegistry

EVALUABLE_OUTCOMES_MAX = 200
FAILURE_EVICTION_WINDOW_HOURS = 24
FAILURE_DEMOTION_THRESHOLD = 3

DISPOSITION_SCORES = {
    "accepted": 1.0,
    "modified": 0.5,
    "rejected": 0.0,
    "resubmitted": 0.0,
    "auto_delivered": 1.0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class CapabilityStateManager:
    def __init__(self, state_path: str = "/var/drnt/state/capabilities.state.json"):
        self._state_path = state_path
        self._state: dict[str, dict] = {}
        self._state_hash: str | None = None

    def initialize_from_registry(self, registry: CapabilityRegistry) -> None:
        """Create fresh state entries for all capabilities. Called when no state file exists."""
        now = _now_iso()
        self._state = {}
        for cap_id, _cap in registry.get_all().items():
            self._state[cap_id] = {
                "capability_id": cap_id,
                "effective_wal_level": 0,
                "status": "active",
                "counters": {
                    "evaluable_outcomes": [],
                    "recent_failures": [],
                    "first_job_date": None,
                    "last_reset": now,
                    "last_incident_source_event_id": None,
                },
            }
        self.save()

    def load(self) -> None:
        """Load existing state file. Compute state_hash."""
        raw = Path(self._state_path).read_bytes()
        self._state_hash = hashlib.sha256(raw).hexdigest()
        self._state = json.loads(raw)

    def save(self) -> None:
        """Persist state to disk atomically. Recompute state_hash."""
        serialized = json.dumps(self._state, indent=2, default=str).encode("utf-8")
        self._state_hash = hashlib.sha256(serialized).hexdigest()

        state_dir = os.path.dirname(self._state_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=state_dir or ".", suffix=".tmp")
        try:
            os.write(fd, serialized)
            os.close(fd)
            os.replace(tmp_path, self._state_path)
        except BaseException:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @property
    def state_hash(self) -> str | None:
        return self._state_hash

    def get(self, capability_id: str) -> dict | None:
        return self._state.get(capability_id)

    def get_effective_wal(self, capability_id: str) -> int:
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        if entry["status"] == "suspended":
            return -1
        return entry["effective_wal_level"]

    def set_effective_wal(self, capability_id: str, level: int) -> None:
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        entry["effective_wal_level"] = level
        self.save()

    def get_status(self, capability_id: str) -> str:
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        return entry["status"]

    # --- Ring Buffer: Evaluable Outcomes ---

    def record_outcome(self, capability_id: str, outcome: dict) -> None:
        """Append an outcome to the ring buffer. FIFO eviction at EVALUABLE_OUTCOMES_MAX."""
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        outcomes = entry["counters"]["evaluable_outcomes"]
        outcomes.append(outcome)
        while len(outcomes) > EVALUABLE_OUTCOMES_MAX:
            outcomes.pop(0)
        self.save()

    def get_outcomes(self, capability_id: str) -> list[dict]:
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        return list(entry["counters"]["evaluable_outcomes"])

    # --- Deque: Recent Failures ---

    def record_failure(self, capability_id: str, timestamp: str, source_event_id: str) -> None:
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        entry["counters"]["recent_failures"].append({
            "timestamp": timestamp,
            "source_event_id": source_event_id,
        })
        self.save()

    def evict_old_failures(self, capability_id: str) -> None:
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        now = datetime.now(timezone.utc)
        cutoff_hours = FAILURE_EVICTION_WINDOW_HOURS
        failures = entry["counters"]["recent_failures"]
        entry["counters"]["recent_failures"] = [
            f for f in failures
            if (now - datetime.fromisoformat(f["timestamp"])).total_seconds()
            < cutoff_hours * 3600
        ]
        self.save()

    def get_recent_failure_count(self, capability_id: str) -> int:
        self.evict_old_failures(capability_id)
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        return len(entry["counters"]["recent_failures"])

    def get_recent_failures(self, capability_id: str) -> list[dict]:
        self.evict_old_failures(capability_id)
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        return list(entry["counters"]["recent_failures"])

    # --- Approval Score ---

    def compute_approval_score(self, capability_id: str) -> float | None:
        """Compute the approval score from evaluable_outcomes.

        Score formula (Trust Profile §9.2):
            accepted = 1.0, modified = 0.5, rejected = 0.0,
            resubmitted = 0.0, auto_delivered = 1.0

        Returns the score as a float, or None if no outcomes exist.
        """
        outcomes = self.get_outcomes(capability_id)
        if not outcomes:
            return None
        total = sum(DISPOSITION_SCORES.get(o["disposition"], 0.0) for o in outcomes)
        return total / len(outcomes)

    # --- First Job Date ---

    def set_first_job_date(self, capability_id: str, timestamp: str) -> None:
        """Set first_job_date if not already set. Called when the first job at a WAL level completes."""
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        if entry["counters"]["first_job_date"] is None:
            entry["counters"]["first_job_date"] = timestamp
            self.save()

    # --- Counter Reset ---

    def reset_counters(self, capability_id: str) -> None:
        """Reset counters per demotion rules. last_incident_source_event_id is PRESERVED."""
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        counters = entry["counters"]
        preserved_incident_id = counters["last_incident_source_event_id"]
        counters["evaluable_outcomes"] = []
        counters["recent_failures"] = []
        counters["first_job_date"] = None
        counters["last_reset"] = _now_iso()
        counters["last_incident_source_event_id"] = preserved_incident_id
        self.save()

    # --- Convenience transitions ---

    def suspend(self, capability_id: str) -> None:
        """Suspend a capability: set effective_wal_level to -1 and status to 'suspended'."""
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        entry["effective_wal_level"] = -1
        entry["status"] = "suspended"
        self.save()

    def demote(self, capability_id: str, to_level: int) -> None:
        """Demote a capability to a specific WAL level.

        If to_level is -1, delegates to suspend(). Otherwise sets
        effective_wal_level and ensures status is 'active'.
        Does NOT reset counters — caller must call reset_counters() separately.
        """
        if to_level == -1:
            self.suspend(capability_id)
            return
        entry = self._state.get(capability_id)
        if entry is None:
            raise KeyError(f"Unknown capability: {capability_id}")
        entry["effective_wal_level"] = to_level
        entry["status"] = "active"
        self.save()

    # --- Bulk operations ---

    def get_all(self) -> dict[str, dict]:
        return dict(self._state)
