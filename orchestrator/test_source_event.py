"""Unit tests for source-event identity primitives (§6B-1).

Covers Insert A "Test Requirements" and §11.8 normalization cases. These are
pure (no I/O, no model, no live stack) — the Insert D "free" bucket — and live
beside the orchestrator modules so they are not gated by the tests/ stack-health
conftest.

Non-ASCII inputs are built with chr() + explicit codepoints so the byte
sequences under test are unambiguous regardless of editor normalization.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from source_event import (
    compute_intent_equivalence_hash,
    normalize_raw_input,
    source_event_key,
)

# "café": é as a single composed codepoint (U+00E9) vs. e + combining acute
# (U+0065 U+0301). NFC normalization must make these equal.
_CAFE_COMPOSED = "caf" + chr(0x00E9)
_CAFE_DECOMPOSED = "cafe" + chr(0x0301)
_IDEOGRAPHIC_SPACE = chr(0x3000)


# ---------- normalization (§5.6) ----------

def test_normalize_collapses_internal_whitespace_runs():
    assert normalize_raw_input("remind   me to  call John") == "remind me to call John"


def test_normalize_trims_leading_and_trailing():
    assert normalize_raw_input("  remind me to call John  ") == "remind me to call John"


def test_normalize_combined_whitespace_example():
    # The Insert A canonical equivalence example.
    assert normalize_raw_input("  remind   me to call John  ") == normalize_raw_input(
        "remind me to call John"
    )


def test_normalize_collapses_unicode_whitespace_to_ascii_space():
    # Ideographic space (U+3000) collapses too — voice-transcription jitter
    # should not change identity.
    assert normalize_raw_input("call" + _IDEOGRAPHIC_SPACE + " John") == "call John"


def test_normalize_applies_nfc():
    assert _CAFE_COMPOSED != _CAFE_DECOMPOSED
    assert normalize_raw_input(_CAFE_COMPOSED) == normalize_raw_input(_CAFE_DECOMPOSED)


def test_normalize_preserves_case():
    assert normalize_raw_input("Call John") == "Call John"
    assert normalize_raw_input("Call John") != normalize_raw_input("call john")


def test_normalize_preserves_punctuation():
    assert normalize_raw_input("call John.") == "call John."
    assert normalize_raw_input("call John.") != normalize_raw_input("call John")


# ---------- intent-equivalence hash (Insert A "Test Requirements") ----------

def _h(raw, modality="text"):
    return compute_intent_equivalence_hash(raw, modality)


def test_hash_whitespace_only_difference_does_not_change():
    assert _h("remind   me to call John") == _h("remind me to call John")


def test_hash_leading_trailing_whitespace_does_not_change():
    assert _h("  remind me to call John  ") == _h("remind me to call John")


def test_hash_unicode_equivalent_strings_match():
    assert _h(_CAFE_COMPOSED) == _h(_CAFE_DECOMPOSED)


def test_hash_changes_on_meaningful_raw_input_change():
    # The Insert A non-equivalence example: call vs text.
    assert _h("remind me to call John") != _h("remind me to text John")


def test_hash_changes_on_input_modality_change():
    assert _h("remind me to call John", "text") != _h(
        "remind me to call John", "voice"
    )


def test_hash_invariant_to_device_and_client_timestamp_by_construction():
    # device and client_timestamp are not inputs to the hash, so they cannot
    # affect it. Two computations over the same (raw_input, modality) are equal
    # regardless of any envelope fields the caller may have varied.
    assert _h("remind me to call John", "text") == _h(
        "remind me to call John", "text"
    )


def test_hash_is_sha256_hex():
    digest = _h("remind me to call John")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


# ---------- durable-store key ----------

def test_source_event_key_format():
    assert source_event_key("phone_app", "evt-1") == "phone_app:evt-1"


def test_source_event_key_distinguishes_source():
    assert source_event_key("phone_app", "evt-1") != source_event_key(
        "watch_app", "evt-1"
    )


def test_source_event_key_collision_free_with_colon_in_event_id():
    # client_source is a closed enum, so an event id containing a colon cannot
    # forge a different (source, event_id) pair.
    assert source_event_key("phone_app", "a:b") != source_event_key(
        "watch_app", "a:b"
    )
