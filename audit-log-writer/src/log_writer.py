"""Main service: Unix domain socket listener and sequential write loop."""

import json
import logging
import os
import signal
import socket
import struct
import threading
import time
from datetime import datetime, timezone

from uuid_utils import uuid7

from .event_validator import validate_event
from .file_manager import FileManager
from .hash_chain import GENESIS_HASH, compute_hash
from .models import AckResponse, CommittedEvent, DuplicateResponse, ErrorResponse

logger = logging.getLogger(__name__)

# 4 bytes, big-endian unsigned int for length prefix
LENGTH_PREFIX = struct.Struct("!I")
MAX_MESSAGE_SIZE = 16 * 1024 * 1024  # 16 MB max message


class AuditLogWriter:
    """Single-threaded append-only audit log writer."""

    def __init__(
        self,
        socket_path: str = "/tmp/drnt-audit.sock",
        log_dir: str = "/var/drnt/audit",
    ):
        self.socket_path = socket_path
        self.log_dir = log_dir
        self.file_manager = FileManager(log_dir)
        self._running = False
        self._server_socket: socket.socket | None = None

        # Chain state
        self._prev_hash: str = GENESIS_HASH
        self._sequence: int = 0
        self._seen_source_ids: set[str] = set()
        self._current_date: str | None = None

        # Best-effort buffer
        self._be_buffer: list[tuple[str, str]] = []  # (date_str, json_line)
        self._last_flush_time: float = time.monotonic()
        self._flush_interval: float = 1.0  # seconds

    def _initialize(self):
        """Initialize chain state from existing files."""
        self._prev_hash, self._sequence, self._seen_source_ids = (
            self.file_manager.recover_chain_state()
        )
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info(
            "Initialized: sequence=%d, dedup_ids=%d, prev_hash=%s...",
            self._sequence,
            len(self._seen_source_ids),
            self._prev_hash[:16],
        )

    def _commit_event(self, data: dict) -> CommittedEvent:
        """Assign writer-owned fields and create a CommittedEvent."""
        now = datetime.now(timezone.utc)
        committed_at = now.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        date_str = now.strftime("%Y-%m-%d")

        # Handle midnight rollover
        if self._current_date is not None and date_str != self._current_date:
            logger.info("Day rollover: %s -> %s", self._current_date, date_str)
            # Flush any buffered best-effort events to the old day
            self._flush_best_effort()
            # prev_hash carries over from the last event of the previous day
            self._current_date = date_str

        self._current_date = date_str
        event_id = str(uuid7())

        event = CommittedEvent(
            schema_version=data["schema_version"],
            event_id=event_id,
            source_event_id=data["source_event_id"],
            timestamp=data["timestamp"],
            committed_at=committed_at,
            event_type=data["event_type"],
            job_id=data.get("job_id"),
            parent_job_id=data.get("parent_job_id"),
            capability_id=data.get("capability_id"),
            wal_level=data.get("wal_level"),
            source=data["source"],
            prev_hash=self._prev_hash,
            durability=data["durability"],
            payload=data["payload"],
        )

        return event

    def _write_event(self, event: CommittedEvent) -> str:
        """Serialize and write an event, updating chain state. Returns the JSON line."""
        json_line = json.dumps(event.to_dict(), separators=(",", ":"), ensure_ascii=False)
        date_str = event.committed_at[:10]

        is_durable = event.durability == "durable"

        if is_durable:
            # Flush any pending best-effort events first
            self._flush_best_effort()
            self.file_manager.append_line(date_str, json_line, fsync=True)
        else:
            self._be_buffer.append((date_str, json_line))

        # Update chain state
        self._prev_hash = compute_hash(json_line)
        self._sequence += 1
        self._seen_source_ids.add(event.source_event_id)

        return json_line

    def _flush_best_effort(self):
        """Flush buffered best-effort events to disk."""
        if not self._be_buffer:
            return
        for date_str, json_line in self._be_buffer:
            self.file_manager.append_line(date_str, json_line, fsync=False)
        self.file_manager.flush()
        self._be_buffer.clear()
        self._last_flush_time = time.monotonic()

    def _maybe_flush_best_effort(self):
        """Flush best-effort buffer if interval has elapsed."""
        if self._be_buffer and (time.monotonic() - self._last_flush_time) >= self._flush_interval:
            self._flush_best_effort()

    def _handle_event(self, data: dict) -> dict:
        """Process a single inbound event and return a response dict."""
        # Validate
        error = validate_event(data)
        if error:
            return ErrorResponse(message=error).to_dict()

        # Deduplicate
        source_event_id = data["source_event_id"]
        if source_event_id in self._seen_source_ids:
            return DuplicateResponse(source_event_id=source_event_id).to_dict()

        # Commit
        event = self._commit_event(data)
        self._write_event(event)

        return AckResponse(
            event_id=event.event_id,
            committed_at=event.committed_at,
            prev_hash=event.prev_hash,
            sequence=self._sequence,
        ).to_dict()

    def _recv_exact(self, conn: socket.socket, n: int) -> bytes:
        """Read exactly n bytes from a socket."""
        data = bytearray()
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError("Connection closed before receiving all data")
            data.extend(chunk)
        return bytes(data)

    def _handle_connection(self, conn: socket.socket):
        """Handle a single client connection (request-response, then close)."""
        try:
            # Read length prefix
            length_bytes = self._recv_exact(conn, 4)
            msg_length = LENGTH_PREFIX.unpack(length_bytes)[0]

            if msg_length > MAX_MESSAGE_SIZE:
                response = ErrorResponse(message="Message too large").to_dict()
            else:
                # Read message body
                msg_bytes = self._recv_exact(conn, msg_length)
                try:
                    data = json.loads(msg_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    response = ErrorResponse(message=f"Invalid JSON: {e}").to_dict()
                else:
                    response = self._handle_event(data)

            # Send response
            resp_bytes = json.dumps(response, separators=(",", ":")).encode("utf-8")
            conn.sendall(LENGTH_PREFIX.pack(len(resp_bytes)) + resp_bytes)

        except ConnectionError:
            logger.debug("Client disconnected")
        except Exception:
            logger.exception("Error handling connection")
        finally:
            conn.close()

    def start(self):
        """Start the audit log writer service."""
        self._initialize()

        # Remove stale socket file
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(self.socket_path)
        self._server_socket.listen(128)
        self._server_socket.settimeout(1.0)  # Allow periodic flush checks
        self._running = True

        logger.info("Audit log writer listening on %s", self.socket_path)

        while self._running:
            try:
                conn, _ = self._server_socket.accept()
                self._handle_connection(conn)
            except socket.timeout:
                pass
            except OSError:
                if self._running:
                    logger.exception("Socket error")
                break

            # Periodic flush of best-effort buffer
            self._maybe_flush_best_effort()

        self._shutdown()

    def stop(self):
        """Signal the writer to stop."""
        logger.info("Shutting down audit log writer...")
        self._running = False

    def _shutdown(self):
        """Clean up resources."""
        self._flush_best_effort()
        self.file_manager.close()
        if self._server_socket:
            self._server_socket.close()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        logger.info("Audit log writer shut down.")


def main():
    """Entry point for the audit log writer service."""
    log_level = os.environ.get("DRNT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    socket_path = os.environ.get("DRNT_AUDIT_SOCKET", "/tmp/drnt-audit.sock")
    log_dir = os.environ.get("DRNT_AUDIT_DIR", "/var/drnt/audit")

    writer = AuditLogWriter(socket_path=socket_path, log_dir=log_dir)

    def handle_signal(signum, frame):
        writer.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    writer.start()


if __name__ == "__main__":
    main()
