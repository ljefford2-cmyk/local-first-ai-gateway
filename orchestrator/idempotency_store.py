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

from source_event import source_event_key

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

# Phase 4A.2.d: separate namespace prefix for review-decision idempotency
# records, distinct from the submit-side ``idem:`` namespace.
_REVIEW_KEY_PREFIX = "review_idem:"

# §6B-1: durable source-event identity namespace. Unlike ``idem:`` (purged for
# terminal jobs on restart), this namespace is the durable "third identity" —
# it is loaded on startup and is NOT matched by ``_purge_terminal_keys`` (which
# matches only ``idem:``), so a source event keeps its identity past terminal
# state and across restarts.
_SOURCE_EVENT_PREFIX = "srcevent:"


@dataclass
class ReviewIdempotencyRecord:
    """Stored review-decision outcome for replay protection."""

    payload_identity: dict
    outcome: dict
    applied: bool
    created_at: datetime


@dataclass
class SourceEventRecord:
    """Durable mapping from a mobile source-event identity to its job.

    Keyed (in the ``state`` table) by ``(client_source, client_source_event_id)``.
    ``intent_equivalence_hash`` distinguishes an *equivalent* replay (same hash
    -> dedup) from a *changed-intent* replay (different hash -> interim 409).
    """

    job_id: str
    intent_equivalence_hash: str
    client_source: str
    client_source_event_id: str
    created_at: datetime


class IdempotencyStore:
    """Thread-safe, TTL-based idempotency key store.

    All mutations are protected by a threading Lock so the store can be
    safely accessed from the async event loop and background tasks.

    When *db_path* is provided (or resolved from the persistence module)
    the store persists records to SQLite and loads them on construction.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._review_records: dict[str, ReviewIdempotencyRecord] = {}
        self._source_event_records: dict[str, SourceEventRecord] = {}
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

        # Phase 4A.2.d: load review-decision records from the same state table
        try:
            cursor = self._db.execute(
                "SELECT key, value FROM state WHERE key LIKE ?",
                (_REVIEW_KEY_PREFIX + "%",),
            )
            for row in cursor:
                review_key = row[0][len(_REVIEW_KEY_PREFIX):]
                data = json.loads(row[1])
                self._review_records[review_key] = ReviewIdempotencyRecord(
                    payload_identity=data["payload_identity"],
                    outcome=data["outcome"],
                    applied=data["applied"],
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
        except Exception:
            logger.warning("Failed to load review idempotency records from DB", exc_info=True)

        # §6B-1: load durable source-event identity records. Never purged, so a
        # source event keeps its identity past terminal state and across restart.
        try:
            cursor = self._db.execute(
                "SELECT key, value FROM state WHERE key LIKE ?",
                (_SOURCE_EVENT_PREFIX + "%",),
            )
            for row in cursor:
                src_key = row[0][len(_SOURCE_EVENT_PREFIX):]
                data = json.loads(row[1])
                self._source_event_records[src_key] = SourceEventRecord(
                    job_id=data["job_id"],
                    intent_equivalence_hash=data["intent_equivalence_hash"],
                    client_source=data["client_source"],
                    client_source_event_id=data["client_source_event_id"],
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
        except Exception:
            logger.warning("Failed to load source-event records from DB", exc_info=True)

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

    # -- Phase 4A.2.d: review-decision idempotency -------------------------

    def _db_write_review(
        self, decision_idempotency_key: str, record: ReviewIdempotencyRecord,
    ) -> None:
        if self._db is None:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            value = json.dumps({
                "payload_identity": record.payload_identity,
                "outcome": record.outcome,
                "applied": record.applied,
                "created_at": record.created_at.isoformat(),
            })
            self._db.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (_REVIEW_KEY_PREFIX + decision_idempotency_key, value, now),
            )
            self._db.commit()
        except Exception:
            logger.warning(
                "DB write failed for review key %s", decision_idempotency_key, exc_info=True,
            )

    def store_review_outcome(
        self,
        decision_idempotency_key: str,
        payload_identity: dict,
        outcome: dict,
        applied: bool,
    ) -> ReviewIdempotencyRecord:
        """Store a review-decision outcome under the review_idem namespace.

        outcome stores the original HTTP response body and status-code
        envelope needed to replay same-key/same-payload without reapplying
        state transitions.
        """
        with self._lock:
            record = ReviewIdempotencyRecord(
                payload_identity=payload_identity,
                outcome=outcome,
                applied=applied,
                created_at=datetime.now(timezone.utc),
            )
            self._review_records[decision_idempotency_key] = record
            self._db_write_review(decision_idempotency_key, record)
            return record

    def get_review_outcome(
        self, decision_idempotency_key: str,
    ) -> Optional[ReviewIdempotencyRecord]:
        """Look up a stored review-decision outcome by key. None if unknown."""
        with self._lock:
            return self._review_records.get(decision_idempotency_key)

    # -- §6B-1/§6B-2: durable source-event identity ------------------------

    @staticmethod
    def _source_event_value(record: SourceEventRecord) -> str:
        return json.dumps({
            "job_id": record.job_id,
            "intent_equivalence_hash": record.intent_equivalence_hash,
            "client_source": record.client_source,
            "client_source_event_id": record.client_source_event_id,
            "created_at": record.created_at.isoformat(),
        })

    @staticmethod
    def _row_to_source_event_record(data: dict) -> SourceEventRecord:
        return SourceEventRecord(
            job_id=data["job_id"],
            intent_equivalence_hash=data["intent_equivalence_hash"],
            client_source=data["client_source"],
            client_source_event_id=data["client_source_event_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    def _db_try_insert_source_event(
        self, src_key: str, record: SourceEventRecord,
    ) -> bool:
        """Atomic first-writer insert via ``ON CONFLICT(key) DO NOTHING``.

        Returns True iff THIS call inserted the row (we are the first writer).
        Returns False on a key conflict (another writer won), when there is no
        DB, or on error — the caller disambiguates conflict-vs-absent by reading.
        Uses a ``total_changes`` delta rather than ``rowcount`` for a reliable
        inserted/skipped signal across SQLite builds.
        """
        if self._db is None:
            return False
        try:
            now = datetime.now(timezone.utc).isoformat()
            before = self._db.total_changes
            self._db.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (_SOURCE_EVENT_PREFIX + src_key, self._source_event_value(record), now),
            )
            self._db.commit()
            return (self._db.total_changes - before) == 1
        except Exception:
            logger.warning(
                "DB reserve failed for source-event key %s", src_key, exc_info=True,
            )
            return False

    def _db_read_source_event(self, src_key: str) -> Optional[SourceEventRecord]:
        if self._db is None:
            return None
        try:
            cursor = self._db.execute(
                "SELECT value FROM state WHERE key = ?",
                (_SOURCE_EVENT_PREFIX + src_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_source_event_record(json.loads(row[0]))
        except Exception:
            logger.warning(
                "DB read failed for source-event key %s", src_key, exc_info=True,
            )
            return None

    def _db_delete_source_event(self, src_key: str, job_id: str) -> None:
        """Compare-and-delete: remove the row only if it still maps to job_id."""
        if self._db is None:
            return
        try:
            self._db.execute(
                "DELETE FROM state WHERE key = ? "
                "AND json_extract(value, '$.job_id') = ?",
                (_SOURCE_EVENT_PREFIX + src_key, job_id),
            )
            self._db.commit()
        except Exception:
            logger.warning(
                "DB delete failed for source-event key %s", src_key, exc_info=True,
            )

    def check_and_store_source_event(
        self,
        client_source: str,
        client_source_event_id: str,
        job_id: str,
        intent_equivalence_hash: str,
    ) -> tuple[str, Optional[SourceEventRecord]]:
        """Atomically classify a source-event submission against durable state.

        First-writer is arbitrated by SQLite (``INSERT ... ON CONFLICT(key) DO
        NOTHING``) when a DB is present, so the decision is correct even across
        processes; without a DB the in-memory map is the arbiter. Returns
        ``(status, record)`` with status one of:

        - ``"new"``        — we won the reservation; ``record`` is ours.
        - ``"equivalent"`` — already reserved with the same intent hash; the
          stored mapping is returned **unchanged**.
        - ``"conflict"``   — already reserved with a different intent hash; the
          stored mapping is left **unchanged**.

        The whole operation runs under the store lock for in-memory consistency.
        A ``new`` reservation is released via ``release_source_event`` if the
        caller fails to materialize the job durably.
        """
        src_key = source_event_key(client_source, client_source_event_id)
        with self._lock:
            record = SourceEventRecord(
                job_id=job_id,
                intent_equivalence_hash=intent_equivalence_hash,
                client_source=client_source,
                client_source_event_id=client_source_event_id,
                created_at=datetime.now(timezone.utc),
            )
            if self._db is not None and self._db_try_insert_source_event(src_key, record):
                self._source_event_records[src_key] = record
                return ("new", record)
            # Either no DB, or we lost the insert — find the authoritative record
            # (the winner may be another process, so the DB row is canonical).
            existing = (
                self._db_read_source_event(src_key) if self._db is not None else None
            ) or self._source_event_records.get(src_key)
            if existing is not None:
                self._source_event_records[src_key] = existing
                if existing.intent_equivalence_hash == intent_equivalence_hash:
                    return ("equivalent", existing)
                return ("conflict", existing)
            # No existing record (no DB, or DB lost-then-vanished). Store ours.
            self._source_event_records[src_key] = record
            return ("new", record)

    def release_source_event(
        self, client_source: str, client_source_event_id: str, job_id: str,
    ) -> None:
        """Roll back a reservation that maps to *job_id* (compare-and-delete).

        Used when a ``new`` reservation's job failed to materialize durably, so a
        later retry of the same source event can create it cleanly rather than
        observing a poisoned mapping. The compare on ``job_id`` ensures we never
        delete a different writer's reservation.
        """
        src_key = source_event_key(client_source, client_source_event_id)
        with self._lock:
            existing = self._source_event_records.get(src_key)
            if existing is not None and existing.job_id == job_id:
                del self._source_event_records[src_key]
            self._db_delete_source_event(src_key, job_id)

    def get_source_event(
        self, client_source: str, client_source_event_id: str,
    ) -> Optional[SourceEventRecord]:
        """Look up a stored source-event mapping (memory then DB). None if unknown."""
        src_key = source_event_key(client_source, client_source_event_id)
        with self._lock:
            record = self._source_event_records.get(src_key)
            if record is not None:
                return record
            record = self._db_read_source_event(src_key)
            if record is not None:
                self._source_event_records[src_key] = record
            return record
