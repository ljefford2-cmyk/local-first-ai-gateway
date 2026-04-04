"""Async client for the DRNT audit log writer (Phase 1).

Communicates over Unix domain socket using length-prefixed JSON.
Protocol: 4-byte big-endian length prefix + UTF-8 JSON payload.
Each event uses a fresh connection (one-shot pattern matching Phase 1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)

LENGTH_PREFIX = struct.Struct("!I")
MAX_RETRIES = 3
TIMEOUT_SECONDS = 5.0
RECONNECT_INITIAL_MS = 100
RECONNECT_MAX_MS = 5000


class AuditLogClient:
    """Client for the DRNT audit log writer."""

    def __init__(self, socket_path: str = "/var/drnt/sockets/audit.sock") -> None:
        self._socket_path = socket_path

    async def _send_and_receive(self, event: dict[str, Any]) -> dict[str, Any]:
        """Open a fresh connection, send one event, receive one response."""
        payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self._socket_path),
            timeout=TIMEOUT_SECONDS,
        )
        try:
            # Send length-prefixed message
            writer.write(LENGTH_PREFIX.pack(len(payload)) + payload)
            await writer.drain()

            # Read 4-byte length prefix
            length_data = await asyncio.wait_for(
                reader.readexactly(4), timeout=TIMEOUT_SECONDS
            )
            (resp_len,) = LENGTH_PREFIX.unpack(length_data)

            # Read response body
            resp_data = await asyncio.wait_for(
                reader.readexactly(resp_len), timeout=TIMEOUT_SECONDS
            )
            return json.loads(resp_data.decode("utf-8"))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def emit_durable(self, event: dict[str, Any]) -> bool:
        """Send a durable event. Blocks until ACK. Returns True on success.

        Retries up to 3 times on connection/timeout errors using the same
        source_event_id (the log writer deduplicates).
        """
        delay_ms = RECONNECT_INITIAL_MS
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = await self._send_and_receive(event)
                status = resp.get("status")
                if status == "ok":
                    return True
                if status == "duplicate":
                    logger.info(
                        "Duplicate event accepted: %s",
                        resp.get("source_event_id"),
                    )
                    return True
                # Validation error from log writer
                logger.error("Audit log writer rejected event: %s", resp)
                return False
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning(
                    "Audit log emit attempt %d/%d failed: %s",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(delay_ms / 1000)
                    delay_ms = min(delay_ms * 2, RECONNECT_MAX_MS)

        raise TimeoutError(
            f"Failed to emit durable event after {MAX_RETRIES} attempts: {last_error}"
        )

    async def emit_best_effort(self, event: dict[str, Any]) -> None:
        """Send a best-effort event. Fire and forget."""
        try:
            await self._send_and_receive(event)
        except Exception as exc:
            logger.debug("Best-effort event send failed (ignored): %s", exc)

    async def health_check(self) -> bool:
        """Returns True if the socket is reachable and the log writer responds."""
        try:
            # Send a minimal well-formed best-effort event as a probe
            from events import build_event

            probe = build_event(
                event_type="system.health_probe",
                job_id=None,
                durability="best_effort",
                payload={"component": "orchestrator", "probe": True},
            )
            resp = await asyncio.wait_for(
                self._send_and_receive(probe), timeout=2.0
            )
            return resp.get("status") in ("ok", "duplicate")
        except Exception:
            return False
