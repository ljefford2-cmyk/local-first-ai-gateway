"""Hub operational state manager for fallback support.

Exactly one hub is authoritative at any time. Switching is a human decision.
Hub suspension signals prevent zombie execution on the demoted hub.
"""

from __future__ import annotations

import logging
import os
import platform
import time
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

    def __init__(
        self,
        emit_event: Optional[Callable[[dict[str, Any]], None]] = None,
        authority_timeout_seconds: Optional[int] = None,
        hostname: Optional[str] = None,
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
        logger.info("Hub state: %s -> suspended", old.value)
        self._try_emit(old, HubState.SUSPENDED, "suspend_command")

    def resume(self) -> None:
        """Transition back to ACTIVE. No-op if already ACTIVE."""
        if self._state == HubState.ACTIVE:
            return
        old = self._state
        self._state = HubState.ACTIVE
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
        logger.info("Hub state: %s -> active (authority confirmed)", old.value)
        self._try_emit(old, HubState.ACTIVE, "authority_confirmed")
        return {"status": "active", "resumed_from": old.value}

    def record_heartbeat(self) -> None:
        """Record a client heartbeat (called on any client request)."""
        self._last_client_heartbeat = time.monotonic()

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
