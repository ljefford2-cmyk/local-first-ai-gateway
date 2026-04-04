"""Daily file rotation and startup chain recovery for the DRNT Audit Log."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hash_chain import GENESIS_HASH, compute_hash

logger = logging.getLogger(__name__)


class FileManager:
    """Manages daily JSONL audit log files with hash chain recovery."""

    def __init__(self, log_dir: str = "/var/drnt/audit"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._current_file = None
        self._current_date: str | None = None

    def get_file_path(self, date_str: str) -> Path:
        return self.log_dir / f"drnt-audit-{date_str}.jsonl"

    def _open_file(self, date_str: str):
        """Open the file for the given date, closing any previously open file."""
        if self._current_file is not None:
            self._current_file.close()
        path = self.get_file_path(date_str)
        self._current_file = open(path, "a", encoding="utf-8")
        self._current_date = date_str
        logger.info("Opened audit log file: %s", path)

    def ensure_file_for_date(self, date_str: str):
        """Ensure we have an open file handle for the given date."""
        if self._current_date != date_str:
            self._open_file(date_str)

    def append_line(self, date_str: str, json_line: str, fsync: bool = False):
        """Append a JSON line to the appropriate daily file."""
        self.ensure_file_for_date(date_str)
        self._current_file.write(json_line + "\n")
        if fsync:
            self._current_file.flush()
            os.fsync(self._current_file.fileno())

    def flush(self):
        """Flush the current file buffer without fsync."""
        if self._current_file is not None:
            self._current_file.flush()

    def close(self):
        """Close the current file handle."""
        if self._current_file is not None:
            self._current_file.close()
            self._current_file = None
            self._current_date = None

    def recover_chain_state(self) -> tuple[str, int, set[str]]:
        """Recover chain state on startup.

        Reads the current and previous day's files to determine:
        - prev_hash: hash of the last written line (or genesis if no files)
        - sequence: next sequence number
        - seen_ids: set of source_event_ids for deduplication

        Returns:
            (prev_hash, next_sequence, seen_source_event_ids)
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        seen_ids: set[str] = set()
        prev_hash = GENESIS_HASH
        sequence = 0

        # Load previous day's file for dedup + chain state
        for date_str in [yesterday, today]:
            path = self.get_file_path(date_str)
            if not path.exists():
                continue

            logger.info("Recovering state from: %s", path)
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        seen_ids.add(event.get("source_event_id", ""))
                        prev_hash = compute_hash(line)
                        sequence += 1
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed line in %s", path)

        # If today's file has events, the genesis for a new day file
        # is determined by the last event of the previous day's file.
        # But if we found events in today's file, prev_hash is already
        # set to the hash of the last line in today's file.

        logger.info(
            "Chain recovery complete: prev_hash=%s..., sequence=%d, dedup_ids=%d",
            prev_hash[:16],
            sequence,
            len(seen_ids),
        )
        return prev_hash, sequence, seen_ids

    def get_last_hash_of_file(self, date_str: str) -> str | None:
        """Get the hash of the last line of a specific day's file.

        Returns None if the file doesn't exist or is empty.
        """
        path = self.get_file_path(date_str)
        if not path.exists():
            return None

        last_line = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if stripped:
                    last_line = stripped

        if last_line is None:
            return None

        return compute_hash(last_line)
