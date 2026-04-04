"""SHA-256 hash chaining logic and genesis seed for the DRNT Audit Log."""

import hashlib

GENESIS_STRING = "DRNT-GENESIS"
GENESIS_HASH = hashlib.sha256(GENESIS_STRING.encode("utf-8")).hexdigest()


def compute_hash(json_line: str) -> str:
    """Compute SHA-256 hex digest of a JSON line (UTF-8 encoded, no trailing newline)."""
    return hashlib.sha256(json_line.encode("utf-8")).hexdigest()


def verify_chain(lines: list[str], expected_genesis: str = GENESIS_HASH) -> tuple[bool, int, str]:
    """Verify hash chain integrity across a list of JSON lines.

    Returns:
        (valid, break_index, message)
        - valid: True if entire chain is valid
        - break_index: -1 if valid, otherwise index of first broken link
        - message: description of result
    """
    import json

    if not lines:
        return True, -1, "Empty file, chain trivially valid"

    # Check first event's prev_hash matches genesis
    first_event = json.loads(lines[0])
    if first_event.get("prev_hash") != expected_genesis:
        return False, 0, (
            f"Event 0: prev_hash mismatch. "
            f"Expected genesis {expected_genesis}, got {first_event.get('prev_hash')}"
        )

    for i in range(1, len(lines)):
        expected_hash = compute_hash(lines[i - 1])
        event = json.loads(lines[i])
        actual_hash = event.get("prev_hash")
        if actual_hash != expected_hash:
            return False, i, (
                f"Event {i}: prev_hash mismatch. "
                f"Expected {expected_hash}, got {actual_hash}"
            )

    return True, -1, f"Chain valid, {len(lines)} events verified"
