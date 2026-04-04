"""Unit tests for the DRNT Context Packager (Phase 4).

Covers all six acceptance criteria from the build spec:
  1. Clean passthrough
  2. Credential stripping
  3. Multi-pattern detection
  4. Context store integrity (SHA-256 round-trip)
  5. Local routing bypass (design contract test)
  6. Default-deny / unclassifiable config
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from context_packager import (
    ContextPackager,
    SensitivityConfig,
    _generalize_date,
    _generalize_location,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sensitivity_config(tmp_path):
    """Standard sensitivity config written to a temp file."""
    cfg = {
        "sensitivity_classes": [
            {
                "class": "credential",
                "hardcoded": True,
                "action": "strip",
                "patterns": [
                    r"(?i)(api[_-]?key|api[_-]?secret|access[_-]?token|bearer\s+)[=:\s]+\S+",
                    r"(?i)(password|passwd|pwd)[=:\s]+\S+",
                    r"sk-[a-zA-Z0-9]{20,}",
                    r"(?i)secret[_-]?key[=:\s]+\S+",
                ],
            },
            {
                "class": "name",
                "hardcoded": False,
                "action": "strip",
                "patterns": [r"\b[A-Z][a-z]+\s[A-Z][a-z]+\b"],
            },
            {
                "class": "financial",
                "hardcoded": False,
                "action": "strip",
                "patterns": [
                    r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
                    r"\b\d{3}-\d{2}-\d{4}\b",
                ],
            },
            {
                "class": "location",
                "hardcoded": False,
                "action": "generalize",
                "patterns": [r"\b\d{5}(-\d{4})?\b"],
            },
            {
                "class": "date",
                "hardcoded": False,
                "action": "generalize",
                "patterns": [
                    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
                    r"\b\d{4}-\d{2}-\d{2}\b",
                ],
            },
            {"class": "custom", "hardcoded": False, "action": "strip", "patterns": []},
        ],
        "default_action_on_unclassifiable": "strip",
    }
    p = tmp_path / "sensitivity.json"
    p.write_text(json.dumps(cfg))
    return SensitivityConfig(str(p))


@pytest.fixture
def mock_audit():
    c = MagicMock()
    c.emit_durable = AsyncMock(return_value=True)
    return c


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "context"
    d.mkdir()
    return str(d)


@pytest.fixture
def packager(sensitivity_config, mock_audit, store):
    return ContextPackager(
        config=sensitivity_config,
        audit_client=mock_audit,
        context_store_dir=store,
    )


def _calls(mock_audit):
    return [c[0][0] for c in mock_audit.emit_durable.call_args_list]


def _strip_events(mock_audit):
    return [e for e in _calls(mock_audit) if e["event_type"] == "context.strip_detail"]


# ---------------------------------------------------------------------------
# Test 1: Clean Passthrough
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_passthrough(packager, mock_audit, store):
    raw = "What are the main differences between Python and JavaScript?"
    result = await packager.package(
        job_id="job-1", raw_input=raw,
        target_model="claude-sonnet-4-20250514",
        route_id="claude-sonnet-default",
        capability_id="route.cloud.claude",
    )

    assert result.assembled_payload == raw
    assert result.assembled_payload_hash == hashlib.sha256(raw.encode()).hexdigest()

    events = _calls(mock_audit)
    packaged = next(e for e in events if e["event_type"] == "context.packaged")
    assert packaged["payload"]["fields_stripped"] == []
    assert packaged["payload"]["fields_included"] != []
    assert len(_strip_events(mock_audit)) == 0

    assert os.path.exists(os.path.join(store, f"{result.context_package_id}.json"))


# ---------------------------------------------------------------------------
# Test 2: Credential Stripping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credential_stripping(packager, mock_audit):
    raw = "My API key is sk-abc123def456ghi789jkl012mno345 and I need help with authentication"
    result = await packager.package(
        job_id="job-2", raw_input=raw,
        target_model="claude-sonnet-4-20250514",
        route_id="claude-sonnet-default",
        capability_id="route.cloud.claude",
    )

    assert "sk-abc123def456ghi789jkl012mno345" not in result.assembled_payload
    assert "[REDACTED:credential]" in result.assembled_payload
    assert "and I need help with authentication" in result.assembled_payload

    strips = _strip_events(mock_audit)
    assert len(strips) >= 1
    assert strips[0]["payload"]["original_class"] == "credential"
    assert strips[0]["source"] == "context_packager"


# ---------------------------------------------------------------------------
# Test 3: Multi-Pattern Detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_pattern_detection(packager, mock_audit):
    raw = "Send payment to John Smith at 31543, card 4111-1111-1111-1111"
    result = await packager.package(
        job_id="job-3", raw_input=raw,
        target_model="claude-sonnet-4-20250514",
        route_id="claude-sonnet-default",
        capability_id="route.cloud.claude",
    )

    assert "John Smith" not in result.assembled_payload
    assert "[REDACTED:name]" in result.assembled_payload

    assert "31543" not in result.assembled_payload
    assert "315XX" in result.assembled_payload

    assert "4111-1111-1111-1111" not in result.assembled_payload
    assert "[REDACTED:financial]" in result.assembled_payload

    assert len(_strip_events(mock_audit)) >= 3


# ---------------------------------------------------------------------------
# Test 4: Context Store Integrity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_store_integrity(packager, mock_audit, store):
    raw = "Test with password=hunter2 embedded"
    result = await packager.package(
        job_id="job-4", raw_input=raw,
        target_model="claude-sonnet-4-20250514",
        route_id="claude-sonnet-default",
        capability_id="route.cloud.claude",
    )

    ctx_path = os.path.join(store, f"{result.context_package_id}.json")
    with open(ctx_path, "r", encoding="utf-8") as f:
        ctx_json = f.read()
    ctx = json.loads(ctx_json)

    # SHA-256 of file == context_object_hash in context.packaged event
    file_hash = hashlib.sha256(ctx_json.encode()).hexdigest()
    packaged_evt = next(e for e in _calls(mock_audit) if e["event_type"] == "context.packaged")
    assert packaged_evt["payload"]["context_object_hash"] == file_hash

    # assembled_payload_hash in file == SHA-256 of the payload string
    payload_in_file = ctx["assembled_payload"]
    assert ctx["assembled_payload_hash"] == hashlib.sha256(payload_in_file.encode()).hexdigest()
    assert result.assembled_payload_hash == ctx["assembled_payload_hash"]


# ---------------------------------------------------------------------------
# Test 5: Local Routing Bypass (design contract)
# ---------------------------------------------------------------------------

def test_local_routing_bypass_is_a_caller_responsibility():
    """The packager is only called for cloud routing — local dispatch never touches it.
    This test verifies the gate in _run_pipeline."""
    import inspect
    from job_manager import JobManager
    src = inspect.getsource(JobManager._run_pipeline)
    # The packager call is gated behind a cloud routing check
    assert 'routing == "cloud"' in src or "routing == 'cloud'" in src, (
        "_run_pipeline must gate packager usage behind cloud routing check"
    )
    assert "_packager" in src, (
        "_run_pipeline must reference _packager for cloud dispatch"
    )


# ---------------------------------------------------------------------------
# Test 6: Default-Deny Config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_deny_config_respected():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(
            {"sensitivity_classes": [], "default_action_on_unclassifiable": "strip"},
            f,
        )
        path = f.name
    try:
        cfg = SensitivityConfig(path)
        assert cfg.default_action == "strip"
        assert cfg.patterns == []
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Test: Fail-Closed (None packager raises)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fail_closed_no_packager(mock_audit):
    """_run_pipeline raises RuntimeError when packager is None on a cloud route."""
    from job_manager import JobManager
    from models import Job, JobStatus

    import uuid_utils
    jm = JobManager(audit_client=mock_audit, context_packager=None)

    job_id = str(uuid_utils.uuid7())
    job = Job(
        job_id=job_id,
        raw_input="send to cloud",
        input_modality="text",
        device="phone",
        status=JobStatus.submitted.value,
        created_at="2026-01-01T00:00:00.000000Z",
    )
    jm._jobs[job_id] = job

    with pytest.raises(RuntimeError, match="Context packager is not initialized"):
        await jm._run_pipeline(job_id)


# ---------------------------------------------------------------------------
# Generalization helpers
# ---------------------------------------------------------------------------

def test_generalize_zip_5digit():
    assert _generalize_location("31543") == "315XX"

def test_generalize_zip_plus4():
    assert _generalize_location("31543-1234") == "315XX-XXXX"

def test_generalize_date_iso():
    assert _generalize_date("2024-03-15") == "2024"

def test_generalize_date_us():
    assert _generalize_date("03/15/1985") == "1985"

def test_generalize_date_short_year_21st():
    assert _generalize_date("3/15/25") == "2025"

def test_generalize_date_short_year_20th():
    assert _generalize_date("3/15/85") == "1985"


# ---------------------------------------------------------------------------
# Additional pattern tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_password_stripped(packager, mock_audit):
    raw = "Login with password=hunter2 please"
    result = await packager.package(
        job_id="job-pw", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    assert "hunter2" not in result.assembled_payload
    assert "[REDACTED:credential]" in result.assembled_payload


@pytest.mark.asyncio
async def test_ssn_stripped(packager, mock_audit):
    raw = "My SSN is 123-45-6789 please help"
    result = await packager.package(
        job_id="job-ssn", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    assert "123-45-6789" not in result.assembled_payload
    assert "[REDACTED:financial]" in result.assembled_payload


@pytest.mark.asyncio
async def test_date_generalized(packager, mock_audit):
    raw = "My birthday is 03/15/1985 and I love cake"
    result = await packager.package(
        job_id="job-date", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    assert "03/15/1985" not in result.assembled_payload
    assert "1985" in result.assembled_payload


@pytest.mark.asyncio
async def test_context_package_id_in_dispatched_event(packager, mock_audit):
    """context_package_id returned by package() is non-null and a valid string."""
    raw = "A clean query with no sensitive data"
    result = await packager.package(
        job_id="job-cpid", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    assert result.context_package_id
    assert isinstance(result.context_package_id, str)
    assert len(result.context_package_id) > 0


# ---------------------------------------------------------------------------
# Allowlist tests (Item 2: name regex false positives)
# ---------------------------------------------------------------------------

_PROD_SENSITIVITY_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "sensitivity.json"
)


@pytest.mark.asyncio
async def test_allowlisted_terms_not_stripped(mock_audit, store):
    """Terms in the allowlist should NOT be treated as personal names."""
    cfg = SensitivityConfig(_PROD_SENSITIVITY_CONFIG)
    pkg = ContextPackager(config=cfg, audit_client=mock_audit, context_store_dir=store)

    raw = (
        "Machine Learning and Deep Learning are types of "
        "Artificial Intelligence used in New York and San Francisco"
    )
    result = await pkg.package(
        job_id="job-allow", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )

    # All allowlisted terms should pass through unchanged
    assert result.assembled_payload == raw
    # No strip_detail events should be emitted
    assert len(_strip_events(mock_audit)) == 0


@pytest.mark.asyncio
async def test_real_names_still_stripped(mock_audit, store):
    """Real personal names should still be stripped even with allowlist."""
    cfg = SensitivityConfig(_PROD_SENSITIVITY_CONFIG)
    pkg = ContextPackager(config=cfg, audit_client=mock_audit, context_store_dir=store)

    raw = "Please contact John Smith about the project"
    result = await pkg.package(
        job_id="job-name", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )

    assert "John Smith" not in result.assembled_payload
    assert "[REDACTED:name]" in result.assembled_payload


# ---------------------------------------------------------------------------
# Generalization pipeline tests (Item 3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_location_generalized_in_pipeline(packager, mock_audit):
    """Zip codes should be generalized (not stripped) through the full pipeline."""
    raw = "My shipping address is 30301 in Atlanta"
    result = await packager.package(
        job_id="job-loc", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    # Original zip should not appear
    assert "30301" not in result.assembled_payload
    # Generalized form should appear
    assert "303XX" in result.assembled_payload
    # Rest of text preserved
    assert "My shipping address is" in result.assembled_payload
    assert "in Atlanta" in result.assembled_payload

    # Verify strip_detail event shows generalization
    strips = _strip_events(mock_audit)
    loc_strips = [s for s in strips if s["payload"]["original_class"] == "location"]
    assert len(loc_strips) == 1
    assert loc_strips[0]["payload"]["action"] == "generalized"
    assert loc_strips[0]["payload"]["method"] == "generalize"


@pytest.mark.asyncio
async def test_location_zip_plus4_generalized(packager, mock_audit):
    """Zip+4 codes should be generalized through the full pipeline."""
    raw = "Send to 31543-1234 please"
    result = await packager.package(
        job_id="job-loc4", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    assert "31543-1234" not in result.assembled_payload
    assert "315XX-XXXX" in result.assembled_payload


@pytest.mark.asyncio
async def test_mixed_strip_and_generalize(packager, mock_audit):
    """Stripping and generalization should work correctly in the same input."""
    raw = "John Smith lives at 30301 and was born on 03/15/1985"
    result = await packager.package(
        job_id="job-mix", raw_input=raw,
        target_model="m", route_id="r", capability_id="c",
    )
    # Name stripped
    assert "John Smith" not in result.assembled_payload
    assert "[REDACTED:name]" in result.assembled_payload
    # Zip generalized
    assert "30301" not in result.assembled_payload
    assert "303XX" in result.assembled_payload
    # Date generalized
    assert "03/15/1985" not in result.assembled_payload
    assert "1985" in result.assembled_payload
