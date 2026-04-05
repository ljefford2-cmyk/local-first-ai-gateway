"""Hub operational state manager for fallback support.

Exactly one hub is authoritative at any time. Switching is a human decision.
Hub suspension signals prevent zombie execution on the demoted hub.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

AUTHORITY_TIMEOUT_SECONDS = int(
    os.environ.get("DRNT_AUTHORITY_TIMEOUT_SECONDS", "600")
)


class HubState(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    AWAITING_AUTHORITY = "awaiting_authority"


class HubStateManager:
    """Manages hub operational state for fallback support.

    Exactly one hub is authoritative at any time. Switching is a
    human decision. Hub suspension signals prevent zombie execution
    on the demoted hub.
    """

    _KEY_STATE = "hub:state"
    _KEY_HEARTBEAT = "hub:last_heartbeat"

    def __init__(
        self,
        emit_event: Optional[Callable[[dict[str, Any]], None]] = None,
        authority_timeout_seconds: Optional[int] = None,
        hostname: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self._state = HubState.ACTIVE
        self._last_client_heartbeat: Optional[float] = None
        self._emit_event = emit_event
        self._authority_timeout = (
            authority_timeout_seconds
            if authority_timeout_seconds is not None
            else AUTHORITY_TIMEOUT_SECONDS
        )
        self._hostname = hostname or platform.node()

        # Resolve db_path: explicit arg → persistence module → None (in-memory)
        if db_path is None:
            try:
                from persistence import get_db_path
                db_path = get_db_path()
            except ImportError:
                pass

        self._db: Optional[sqlite3.Connection] = None
        if db_path is not None:
            try:
                self._db = sqlite3.connect(db_path, check_same_thread=False)
                self._db.execute("SELECT 1 FROM state LIMIT 1")
                logger.info("Hub state store using SQLite at %s", db_path)
            except Exception:
                logger.warning(
                    "SQLite unavailable — falling back to in-memory hub state",
                    exc_info=True,
                )
                self._db = None

        self._load_from_db()

    # -- persistence helpers --

    def _load_from_db(self) -> None:
        """Load hub state and heartbeat from the database."""
        if self._db is None:
            return
        try:
            row = self._db.execute(
                "SELECT value FROM state WHERE key = ?", (self._KEY_STATE,)
            ).fetchone()
            if row:
                data = json.loads(row[0])
                self._state = HubState(data["state"])

            row = self._db.execute(
                "SELECT value FROM state WHERE key = ?", (self._KEY_HEARTBEAT,)
            ).fetchone()
            if row:
                data = json.loads(row[0])
                # Convert stored wall-clock ISO timestamp back to monotonic offset
                wall_ts = datetime.fromisoformat(data["wall_timestamp"])
                elapsed = (datetime.now(timezone.utc) - wall_ts).total_seconds()
                self._last_client_heartbeat = time.monotonic() - elapsed

            logger.info("Loaded hub state from database: %s", self._state.value)
        except Exception:
            logger.warning("Failed to load hub state from DB", exc_info=True)

    def _db_write_state(self) -> None:
        """Write-through current hub state to SQLite."""
        if self._db is None:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            value = json.dumps({"state": self._state.value})
            self._db.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (self._KEY_STATE, value, now),
            )
            self._db.commit()
        except Exception:
            logger.warning("DB write failed for hub state", exc_info=True)

    def _db_write_heartbeat(self) -> None:
        """Write-through last heartbeat timestamp to SQLite."""
        if self._db is None:
            return
        try:
            now = datetime.now(timezone.utc)
            # Store wall-clock time so it survives restart
            offset = time.monotonic() - self._last_client_heartbeat
            wall = now - timedelta(seconds=offset)
            value = json.dumps({"wall_timestamp": wall.isoformat()})
            self._db.execute(
                "INSERT OR REPLACE INTO state (key, value, updated_at) VALUES (?, ?, ?)",
                (self._KEY_HEARTBEAT, value, now.isoformat()),
            )
            self._db.commit()
        except Exception:
            logger.warning("DB write failed for hub heartbeat", exc_info=True)

    @property
    def state(self) -> HubState:
        return self._state

    @property
    def hostname(self) -> str:
        return self._hostname

    def is_processing_allowed(self) -> bool:
        """Returns True only if state is ACTIVE."""
        return self._state == HubState.ACTIVE

    def suspend(self) -> None:
        """Transition to SUSPENDED. Idempotent."""
        if self._state == HubState.SUSPENDED:
            return
        old = self._state
        self._state = HubState.SUSPENDED
        self._db_write_state()
        logger.info("Hub state: %s -> suspended", old.value)
        self._try_emit(old, HubState.SUSPENDED, "suspend_command")

    def resume(self) -> None:
        """Transition back to ACTIVE. No-op if already ACTIVE."""
        if self._state == HubState.ACTIVE:
            return
        old = self._state
        self._state = HubState.ACTIVE
        self._db_write_state()
        logger.info("Hub state: %s -> active", old.value)
        self._try_emit(old, HubState.ACTIVE, "resume_command")

    def confirm_authority(self) -> dict[str, str]:
        """Client confirms this hub is authoritative.

        Returns a status dict. Transitions from AWAITING_AUTHORITY to ACTIVE.
        No-op if already ACTIVE.
        """
        if self._state == HubState.ACTIVE:
            return {"status": "active", "resumed_from": "already_active"}
        old = self._state
        self._state = HubState.ACTIVE
        self._db_write_state()
        logger.info("Hub state: %s -> active (authority confirmed)", old.value)
        self._try_emit(old, HubState.ACTIVE, "authority_confirmed")
        return {"status": "active", "resumed_from": old.value}

    def record_heartbeat(self) -> None:
        """Record a client heartbeat (called on any client request)."""
        self._last_client_heartbeat = time.monotonic()
        self._db_write_heartbeat()

    def seconds_since_last_heartbeat(self) -> Optional[float]:
        """Elapsed time since last heartbeat, or None if no heartbeat recorded."""
        if self._last_client_heartbeat is None:
            return None
        return time.monotonic() - self._last_client_heartbeat

    def startup_self_check(self) -> None:
        """Run startup self-check.

        If no heartbeat has been received within the authority timeout,
        transition to AWAITING_AUTHORITY. Otherwise stay ACTIVE.
        """
        elapsed = self.seconds_since_last_heartbeat()
        if elapsed is None or elapsed > self._authority_timeout:
            self._state = HubState.AWAITING_AUTHORITY
            self._db_write_state()
            logger.info(
                "Startup self-check: no recent heartbeat (elapsed=%s, timeout=%d) -> awaiting_authority",
                elapsed,
                self._authority_timeout,
            )
        else:
            logger.info(
                "Startup self-check: recent heartbeat (elapsed=%.1fs) -> staying active",
                elapsed,
            )

    def last_client_heartbeat_iso(self) -> Optional[str]:
        """Return last heartbeat as an approximate ISO timestamp, or None."""
        if self._last_client_heartbeat is None:
            return None
        import datetime

        # Convert monotonic offset to wall-clock approximation
        offset = time.monotonic() - self._last_client_heartbeat
        wall = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=offset)
        return wall.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _try_emit(self, from_state: HubState, to_state: HubState, reason: str) -> None:
        if self._emit_event is None:
            return
        try:
            from events import event_hub_state_change

            self._emit_event(
                event_hub_state_change(
                    from_state=from_state.value,
                    to_state=to_state.value,
                    reason=reason,
                    hostname=self._hostname,
                )
            )
        except Exception:
            logger.warning("Failed to emit hub_state_change event", exc_info=True)
