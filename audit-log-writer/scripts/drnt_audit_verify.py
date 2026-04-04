#!/usr/bin/env python3
"""Standalone chain verification script for DRNT Audit Log files.

Reads one or more JSONL files in order, recomputes every hash from genesis,
and reports any chain breaks with the exact event where the break occurs.

Usage:
    python drnt_audit_verify.py /var/drnt/audit/drnt-audit-2025-01-01.jsonl [more files...]
    python drnt_audit_verify.py /var/drnt/audit/  # verifies all files in sorted order
"""

import hashlib
import json
import os
import sys
from pathlib import Path

GENESIS_STRING = "DRNT-GENESIS"
GENESIS_HASH = hashlib.sha256(GENESIS_STRING.encode("utf-8")).hexdigest()


def compute_hash(json_line: str) -> str:
    return hashlib.sha256(json_line.encode("utf-8")).hexdigest()


def collect_files(paths: list[str]) -> list[Path]:
    """Resolve paths to an ordered list of JSONL files."""
    files = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.glob("drnt-audit-*.jsonl")))
        elif path.is_file():
            files.append(path)
        else:
            print(f"WARNING: {p} does not exist, skipping", file=sys.stderr)
    return sorted(set(files))


def verify_files(files: list[Path]) -> bool:
    """Verify hash chain across all files in order.

    Returns True if chain is valid, False otherwise.
    """
    if not files:
        print("No files to verify.")
        return True

    prev_hash = GENESIS_HASH
    total_events = 0
    all_valid = True

    for file_path in files:
        print(f"\nVerifying: {file_path}")
        line_num = 0
        file_events = 0

        with open(file_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                line_num += 1
                total_events += 1
                file_events += 1

                try:
                    event = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"  BREAK at line {line_num}: malformed JSON — {e}")
                    all_valid = False
                    # Can't continue chain verification after malformed line
                    prev_hash = None
                    continue

                event_id = event.get("event_id", "unknown")
                event_prev_hash = event.get("prev_hash", "")

                if prev_hash is None:
                    # Previous line was malformed, can't verify this link
                    print(f"  SKIP line {line_num} (event_id={event_id}): "
                          f"cannot verify — previous line was malformed")
                    prev_hash = compute_hash(line)
                    continue

                # Check if first event in first file should chain from genesis
                if event_prev_hash != prev_hash:
                    print(
                        f"  BREAK at line {line_num} (event_id={event_id}):\n"
                        f"    expected prev_hash: {prev_hash}\n"
                        f"    actual prev_hash:   {event_prev_hash}"
                    )
                    all_valid = False

                prev_hash = compute_hash(line)

        print(f"  {file_events} events in file")

    print(f"\n{'='*60}")
    if all_valid:
        print(f"RESULT: Chain valid, {total_events} events verified across {len(files)} file(s)")
    else:
        print(f"RESULT: Chain BROKEN — {total_events} events checked across {len(files)} file(s)")

    return all_valid


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file_or_directory> [more files...]")
        print(f"       {sys.argv[0]} /var/drnt/audit/")
        sys.exit(1)

    files = collect_files(sys.argv[1:])
    if not files:
        print("No JSONL files found.")
        sys.exit(1)

    valid = verify_files(files)
    sys.exit(0 if valid else 1)


if __name__ == "__main__":
    main()
