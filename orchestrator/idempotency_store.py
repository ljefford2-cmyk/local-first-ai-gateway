"""TTL-based idempotency key store (Phase 7B).

Every request submitted from a client device carries a client-generated
UUIDv7 idempotency key. The store retains keys for at least 7 days
(configurable). Re-submission with a known key returns the existing
job_id instead of creating a duplicate.

V1 scope: in-memory only — persistence is a future concern.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class IdempotencyRecord:
    """Single entry in the idempotency store."""

    job_id: str
    created_at: datetime
    status: str  # mirrors job status or "accepted"


# Default TTL: 7 days in seconds
DEFAULT_TTL_SECONDS = 604800


class IdempotencyStore:
    """Thread-safe, TTL-based idempotency key store.

    All mutations are protected by a threading Lock so the store can be
    safely accessed from the async event loop and background tasks.
    """

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    # -- public API --

    def check_and_store(
        self, idempotency_key: str, job_id: str
    ) -> tuple[bool, Optional[str]]:
        """Check whether *idempotency_key* is already known.

        Returns ``(True, None)`` if the key is new (and stores it).
        Returns ``(False, existing_job_id)`` if the key already exists.
        """
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                return (False, existing.job_id)
            self._records[idempotency_key] = IdempotencyRecord(
                job_id=job_id,
                created_at=datetime.now(timezone.utc),
                status="accepted",
            )
            return (True, None)

    def update_status(self, idempotency_key: str, status: str) -> None:
        """Update the stored status for a key.

        No-op if the key is unknown (no exception raised).
        """
        with self._lock:
            record = self._records.get(idempotency_key)
            if record is not None:
                record.status = status

    def get(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Look up a record by idempotency key. Returns None if not found."""
        with self._lock:
            return self._records.get(idempotency_key)

    def purge_expired(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> int:
        """Remove entries older than *ttl_seconds*.

        Returns the number of purged entries. The caller is responsible
        for invoking this periodically — no auto-purge.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            expired_keys = [
                key
                for key, rec in self._records.items()
                if (now - rec.created_at).total_seconds() > ttl_seconds
            ]
            for key in expired_keys:
                del self._records[key]
            return len(expired_keys)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
