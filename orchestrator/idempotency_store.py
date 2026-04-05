"""TTL-based idempotency key store (Phase 7B).

Every request submitted from a client device carries a client-generated
UUIDv7 idempotency key. The store retains keys for at least 7 days
(configurable). Re-submission with a known key returns the existing
job_id instead of creating a duplicate.

Persistence: records are written through to SQLite (via persistence.py)
when a database is available.  If the database is unavailable the store
falls back to pure in-memory operation — losing dedup is preferred over
refusing all jobs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IdempotencyRecord:
    """Single entry in the idempotency store."""

    job_id: str
    created_at: datetime
    status: str  # mirrors job status or "accepted"


# Default TTL: 7 days in seconds
DEFAULT_TTL_SECONDS = 604800

# Key prefix used in the generic ``state`` table to namespace
# idempotency records and support efficient bulk queries.
_KEY_PREFIX = "idem:"


class IdempotencyStore:
    """Thread-safe, TTL-based idempotency key store.

    All mutations are protected by a threading Lock so the store can be
    safely accessed from the async event loop and background tasks.

    When *db_path* is provided (or resolved from the persistence module)
    the store persists records to SQLite and loads them on construction.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

        # Resolve db_path: explicit arg → persistence module → None (in-memory)
        if db_path is None:
            try:
                from persistence import get_db_path
                db_path = get_db_path()
            except ImportError:
                pass

        if db_path is not None:
            try:
                self._db = sqlite3.connect(db_path, check_same_thread=False)
                # Verify the state table exists
                self._db.execute("SELECT 1 FROM state LIMIT 1")
                self._load_from_db()
                logger.info("Idempotency store using SQLite at %s", db_path)
            except Exception:
                logger.warning(
                    "SQLite unavailable — falling back to in-memory idempotency store",
                    exc_info=True,
                )
                self._db = None

    # -- internal helpers --------------------------------------------------

    def _purge_terminal_keys(self) -> int:
        """Delete idempotency keys referencing terminal or missing jobs.

        Every prompt is a new routing event.  Once a job reaches terminal
        state (delivered/failed) the idempotency key is architecturally
        invalid — honouring it would bind a fresh prompt to a stale routing
        decision from a previous session.  Keys whose job no longer exists
        in the jobs table are equally stale.

        Returns the number of purged keys.
        """
        if self._db is None:
            return 0
        try:
            # Single SQL operation: delete idem keys where the referenced
            # job is terminal or no longer exists in the jobs table.
            cursor = self._db.execute(
                """DELETE FROM state
                   WHERE key LIKE ?
                     AND NOT EXISTS (
                         SELECT 1 FROM jobs
                          WHERE jobs.job_id = json_extract(state.value, '$.job_id')
                            AND jobs.status NOT IN ('delivered', 'failed')
                     )""",
                (_KEY_PREFIX + "%",),
            )
            count = cursor.rowcount
            self._db.commit()
            return count
        except Exception:
            logger.warning("Failed to purge terminal idempotency keys", exc_info=True)
            return 0

    def _load_from_db(self) -> None:
        """Populate the in-memory cache from the database."""
        if self._db is None:
            return

        # Purge stale keys before loading — see _purge_terminal_keys docstring.
        purged = self._purge_terminal_keys()
        if purged:
            logger.info("Startup: purged %d stale idempotency key(s) from previous sessions", purged)

        try:
            cursor = self._db.execute(
                "SELECT key, value FROM state WHERE key LIKE ?",
                (_KEY_PREFIX + "%",),
            )
            for row in cursor:
                idem_key = row[0][len(_KEY_PREFIX):]
                data = json.loads(row[1])
                self._records[idem_key] = IdempotencyRecord(
                    job_id=data["job_id"],
                    created_at=datetime.fromisoformat(data["created_at"]),
                    status=data["status"],
                )
        except Exception:
            logger.warning("Failed to load idempotency records from DB", exc_info=True)

    def _db_write(self, idempotency_key: str, record: IdempotencyRecord) -> None:
        """Write-through a single record to SQLite.  Failures are logged
        but never block the caller."""
        if self._db is None:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            value = json.dumps({
                "job_id": record.job_id,
                "status": record.status,
                "created_at": record.created_at.isoformat(),
            })
            self._db.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (_KEY_PREFIX + idempotency_key, value, now),
            )
            self._db.commit()
        except Exception:
            logger.warning("DB write failed for key %s", idempotency_key, exc_info=True)

    def _db_delete(self, keys: list[str]) -> None:
        """Delete records from SQLite.  Failures are logged but never
        block the caller."""
        if self._db is None or not keys:
            return
        try:
            placeholders = ",".join("?" * len(keys))
            db_keys = [_KEY_PREFIX + k for k in keys]
            self._db.execute(
                f"DELETE FROM state WHERE key IN ({placeholders})",
                db_keys,
            )
            self._db.commit()
        except Exception:
            logger.warning("DB delete failed", exc_info=True)

    # -- public API --------------------------------------------------------

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
            self._db_write(idempotency_key, self._records[idempotency_key])
            return (True, None)

    def update_status(self, idempotency_key: str, status: str) -> None:
        """Update the stored status for a key.

        No-op if the key is unknown (no exception raised).
        """
        with self._lock:
            record = self._records.get(idempotency_key)
            if record is not None:
                record.status = status
                self._db_write(idempotency_key, record)

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
            self._db_delete(expired_keys)
            return len(expired_keys)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
