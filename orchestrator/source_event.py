"""Source-event identity primitives (§6B-1).

Pure, dependency-light helpers for the mobile source-event identity slice:

- ``normalize_raw_input`` — the §5.6 deterministic normalized form of a
  ``raw_input`` string (Unicode NFC, trim, collapse internal whitespace runs
  to a single ASCII space; case and punctuation preserved).
- ``compute_intent_equivalence_hash`` — SHA-256 over the canonical
  ``{normalized_raw_input, input_modality}`` pair. This is the durable
  intent-equivalence identity used to distinguish *equivalent* replays of a
  source event from *changed-intent* replays.
- ``source_event_key`` — the durable-store key for a source-event identity.

These functions are intentionally free of I/O, model calls, and orchestrator
state so they can be unit-tested with neither the live stack nor the local
model (Insert D "free" bucket).

Stability note: the output of ``compute_intent_equivalence_hash`` is a durable
identity. Its definition (normalization + inputs + canonical JSON + SHA-256) is
contract — changing any part is a breaking ADR change.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

# Matches one or more Unicode whitespace characters. ``re`` operates on ``str``
# in Unicode mode by default, so this also collapses non-breaking and
# ideographic spaces (common voice-transcription jitter), not only ASCII.
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_raw_input(raw_input: str) -> str:
    """Return the §5.6 normalized form of *raw_input*.

    Steps, in order:
        1. Unicode NFC normalization.
        2. Collapse every internal whitespace run to a single ASCII space.
        3. Trim leading/trailing whitespace.

    Case and punctuation are preserved.

    Examples:
        ``"  remind   me to call John  "`` -> ``"remind me to call John"``
        ``"remind me to call John"``       -> ``"remind me to call John"``  (equivalent)
        ``"remind me to text John"``       -> ``"remind me to text John"``  (non-equivalent)
    """
    normalized = unicodedata.normalize("NFC", raw_input)
    normalized = _WHITESPACE_RUN.sub(" ", normalized)
    return normalized.strip()


def compute_intent_equivalence_hash(raw_input: str, input_modality: str) -> str:
    """Compute the intent-equivalence hash for a source event.

    The hash is taken over exactly ``{normalized_raw_input, input_modality}``.
    Nothing else (device, client_timestamp, idempotency_key, client_source,
    client_source_event_id, transport metadata, ...) participates — two source
    events with the same normalized intent and modality share a hash regardless
    of envelope drift.

    Returns a 64-char lowercase hex SHA-256 digest.
    """
    payload = {
        "normalized_raw_input": normalize_raw_input(raw_input),
        "input_modality": input_modality,
    }
    # Canonical JSON: sorted keys, no insignificant whitespace, raw Unicode
    # (the normalized text is already NFC-canonical, so the bytes are stable).
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def source_event_key(client_source: str, client_source_event_id: str) -> str:
    """Build the durable-store key for a source-event identity.

    Collision-free because ``client_source`` is a closed enum
    (``phone_app`` / ``watch_app``), so no client-supplied event id can produce
    an ambiguous join even when it contains a colon.
    """
    return f"{client_source}:{client_source_event_id}"
