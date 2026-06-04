"""Unit tests for audit-log integrity verification and tail-repair (Patch 0).

Covers the five required recovery scenarios plus byte-exactness and CLI checks:
  1. Healthy chain verifies and the startup gate passes.
  2. Empty audit volume initializes (first-run) and passes.
  3. Broken tail is classified repairable, repaired byte-exactly, re-verifies.
  4. Interior corruption is refused (fail closed); nothing is mutated.
  5. The e2e preflight detects the audit_integrity failure signature.

Lives under orchestrator/ (not tests/) so the live-stack health gate in
tests/conftest.py does not skip these pure unit tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from audit_integrity import (
    AUDIT_FAILURE_MARKER,
    GENESIS_HASH,
    RepairRefused,
    classify,
    collect_log_files,
    compute_hash,
    format_startup_failure,
    is_audit_startup_failure,
    main,
    read_records,
    repair_tail,
    verify_path,
)
from startup_validator import HubConfig, StartupValidator


# ---------------------------------------------------------------------------
# Helpers — build valid and corrupted chains at the byte level (LF only, so
# the tests are byte-exact on Windows as well as Linux).
# ---------------------------------------------------------------------------


def _write_chain(audit_dir: Path, count: int, date: str = "2026-06-04") -> Path:
    """Write a valid hash-chained JSONL audit file with `count` records."""
    path = audit_dir / f"drnt-audit-{date}.jsonl"
    prev = GENESIS_HASH
    lines = []
    for i in range(count):
        evt = {
            "event_id": f"evt-{i:04d}",
            "sequence": i,
            "event_type": "system.startup" if i == 0 else "test.event",
            "payload": {"index": i},
            "prev_hash": prev,
        }
        line = json.dumps(evt, separators=(",", ":"))
        lines.append(line)
        prev = compute_hash(line)
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def _read_lines(path: Path) -> list[str]:
    text = path.read_bytes().decode("utf-8")
    return [ln for ln in text.split("\n") if ln != ""]


def _write_lines(path: Path, lines: list[str], trailing_newline: bool = True) -> None:
    body = "\n".join(lines)
    if trailing_newline:
        body += "\n"
    path.write_bytes(body.encode("utf-8"))


def _continue_chain(
    audit_dir: Path, prev_hash: str, count: int, date: str
) -> Path:
    """Write a file whose first record continues from `prev_hash` (no re-anchor)."""
    path = audit_dir / f"drnt-audit-{date}.jsonl"
    prev = prev_hash
    lines = []
    for i in range(count):
        evt = {"event_id": f"cont-{i:04d}", "payload": {"index": i}, "prev_hash": prev}
        line = json.dumps(evt, separators=(",", ":"))
        lines.append(line)
        prev = compute_hash(line)
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return path


def _config(audit_dir: Path) -> HubConfig:
    return HubConfig(audit_log_dir=str(audit_dir), hash_chain_check_count=100)


# ===========================================================================
# Scenario 1 — healthy chain
# ===========================================================================


class TestHealthyChain:
    def test_verify_valid_chain(self, tmp_path):
        _write_chain(tmp_path, 10)
        report = verify_path(tmp_path)
        assert report.status == "valid"
        assert report.records_total == 10
        assert report.records_valid == 10
        assert report.first_invalid_record is None
        assert report.last_valid_record == 9
        assert report.repairable is False

    def test_startup_gate_passes_on_valid_chain(self, tmp_path):
        _write_chain(tmp_path, 10)
        result = StartupValidator(_config(tmp_path)).check_audit_integrity()
        assert result.passed is True
        assert result.check_name == "audit_integrity"
        assert result.details["hash_chain_intact"] is True

    def test_no_repair_manifest_for_valid_chain(self, tmp_path):
        _write_chain(tmp_path, 5)
        with pytest.raises(RepairRefused):
            repair_tail(tmp_path)
        assert not (tmp_path / "quarantine").exists()


# ===========================================================================
# Scenario 2 — empty audit volume
# ===========================================================================


class TestEmptyVolume:
    def test_verify_empty_dir(self, tmp_path):
        report = verify_path(tmp_path)
        assert report.status == "empty"
        assert report.records_total == 0
        assert report.last_valid_hash == GENESIS_HASH
        assert report.repairable is False

    def test_startup_gate_first_run_passes(self, tmp_path):
        result = StartupValidator(_config(tmp_path)).check_audit_integrity()
        assert result.passed is True
        assert result.details["first_run"] is True
        assert "First run" in result.message


# ===========================================================================
# Scenario 3 — broken tail is repairable
# ===========================================================================


class TestBrokenTail:
    def test_verify_classifies_broken_tail(self, tmp_path):
        path = _write_chain(tmp_path, 8)
        lines = _read_lines(path)
        last = json.loads(lines[-1])
        last["prev_hash"] = "0" * 64  # break the last link, still valid JSON
        lines[-1] = json.dumps(last, separators=(",", ":"))
        _write_lines(path, lines)

        report = verify_path(tmp_path)
        assert report.status == "broken_tail"
        assert report.repairable is True
        assert report.first_invalid_record == 7
        assert report.records_valid == 7
        assert report.last_valid_record == 6

    def test_repair_is_byte_exact_and_reverifies(self, tmp_path):
        path = _write_chain(tmp_path, 8)
        lines = _read_lines(path)
        last = json.loads(lines[-1])
        last["prev_hash"] = "0" * 64
        lines[-1] = json.dumps(last, separators=(",", ":"))
        _write_lines(path, lines)

        # Capture the exact cut point before repair.
        recs = read_records(collect_log_files(tmp_path))
        report = classify(recs, collect_log_files(tmp_path))
        cut = recs[report.first_invalid_record].byte_start
        original = path.read_bytes()

        manifest = repair_tail(tmp_path, out=tmp_path / "repair-report.json")

        # Valid prefix preserved verbatim; corrupt suffix quarantined verbatim.
        assert path.read_bytes() == original[:cut]
        quarantine = Path(manifest["corrupt_suffix_path"])
        assert quarantine.read_bytes() == original[cut:]
        assert (
            manifest["corrupt_suffix_sha256"]
            == hashlib.sha256(original[cut:]).hexdigest()
        )

        # Manifest content.
        assert manifest["repair_type"] == "tail_quarantine"
        assert manifest["corrupt_from_record_index"] == 7
        assert manifest["last_valid_record_index"] == 6
        assert manifest["last_valid_hash"] == report.last_valid_hash
        assert manifest["operator_required"] is True
        assert manifest["post_repair_status"] == "valid"
        assert (tmp_path / "repair-report.json").exists()

        # Post-repair verification passes.
        assert verify_path(tmp_path).status == "valid"
        assert verify_path(tmp_path).records_total == 7

    def test_torn_final_write_is_repairable(self, tmp_path):
        """A truncated, newline-less final line (interrupted write) is a tail."""
        path = _write_chain(tmp_path, 5)
        original = path.read_bytes()
        with open(path, "ab") as f:
            f.write(b'{"event_id":"evt-partial","prev')  # truncated, no newline

        report = verify_path(tmp_path)
        assert report.status == "broken_tail"
        assert report.repairable is True
        assert report.first_invalid_record == 5

        manifest = repair_tail(tmp_path)
        assert path.read_bytes() == original  # sealed back to the 5 valid records
        assert Path(manifest["corrupt_suffix_path"]).read_bytes() == (
            b'{"event_id":"evt-partial","prev'
        )
        assert verify_path(tmp_path).status == "valid"

    def test_startup_message_is_actionable(self, tmp_path):
        path = _write_chain(tmp_path, 8)
        lines = _read_lines(path)
        last = json.loads(lines[-1])
        last["prev_hash"] = "0" * 64
        lines[-1] = json.dumps(last, separators=(",", ":"))
        _write_lines(path, lines)

        result = StartupValidator(_config(tmp_path)).check_audit_integrity()
        assert result.passed is False
        assert result.severity == "critical"
        assert result.details["audit_status"] == "broken_tail"
        assert result.details["repairable"] is True
        assert AUDIT_FAILURE_MARKER in result.message
        assert "Repairable: yes" in result.message
        assert "No audit data was deleted" in result.message
        assert "repair-tail" in result.message


# ===========================================================================
# Scenario 4 — interior corruption is not auto-repaired
# ===========================================================================


class TestInteriorCorruption:
    def _make_interior(self, tmp_path: Path) -> Path:
        path = _write_chain(tmp_path, 8)
        lines = _read_lines(path)
        # Edit a middle record's content but leave its prev_hash intact: the
        # break surfaces at the *next* record while the suffix stays coherent.
        mid = json.loads(lines[3])
        mid["payload"] = {"index": 999}
        lines[3] = json.dumps(mid, separators=(",", ":"))
        _write_lines(path, lines)
        return path

    def test_verify_classifies_interior(self, tmp_path):
        self._make_interior(tmp_path)
        report = verify_path(tmp_path)
        assert report.status == "interior_corruption"
        assert report.repairable is False
        assert report.first_invalid_record == 4

    def test_repair_refused_and_nothing_mutated(self, tmp_path):
        path = self._make_interior(tmp_path)
        original = path.read_bytes()
        with pytest.raises(RepairRefused):
            repair_tail(tmp_path)
        assert path.read_bytes() == original
        assert not (tmp_path / "quarantine").exists()

    def test_first_record_bad_genesis_is_interior(self, tmp_path):
        """No trusted prefix -> fail closed, even if the rest is self-consistent."""
        path = _write_chain(tmp_path, 5)
        lines = _read_lines(path)
        first = json.loads(lines[0])
        first["prev_hash"] = "f" * 64  # not genesis
        lines[0] = json.dumps(first, separators=(",", ":"))
        _write_lines(path, lines)
        report = verify_path(tmp_path)
        assert report.status == "interior_corruption"
        assert report.repairable is False

    def test_startup_message_interior(self, tmp_path):
        self._make_interior(tmp_path)
        result = StartupValidator(_config(tmp_path)).check_audit_integrity()
        assert result.passed is False
        assert result.severity == "critical"
        assert result.details["audit_status"] == "interior_corruption"
        assert "interior corruption" in result.message.lower()
        assert "Repairable: no" in result.message


# ===========================================================================
# Segment-aware verification (the actual fix)
# ===========================================================================


class TestSegmentedChain:
    def test_single_continuous_segment_validates(self, tmp_path):
        _write_chain(tmp_path, 12)
        report = verify_path(tmp_path)
        assert report.status == "valid"
        assert report.records_total == 12

    def test_multiple_genesis_segments_validate(self, tmp_path):
        # Two files, each starting at genesis = two legitimate segments (what the
        # writer produces after a multi-day gap). Must validate.
        _write_chain(tmp_path, 5, date="2026-06-01")
        _write_chain(tmp_path, 4, date="2026-06-04")  # genesis re-anchor
        report = verify_path(tmp_path)
        assert report.status == "valid"
        assert report.records_total == 9
        assert "2 segment" in report.message

    def test_continuous_chain_across_files_validates(self, tmp_path):
        # No gap: the second file continues from the first file's last hash.
        _write_chain(tmp_path, 5, date="2026-06-01")
        head = verify_path(tmp_path).last_valid_hash
        _continue_chain(tmp_path, head, 4, date="2026-06-02")
        report = verify_path(tmp_path)
        assert report.status == "valid"
        assert report.records_total == 9
        assert "1 segment" in report.message

    def test_genesis_mid_file_is_rejected(self, tmp_path):
        # GUARDRAIL: genesis is valid ONLY at a segment start, never mid-segment.
        path = _write_chain(tmp_path, 6)
        lines = _read_lines(path)
        mid = json.loads(lines[3])
        mid["prev_hash"] = GENESIS_HASH  # genesis at a non-boundary record
        lines[3] = json.dumps(mid, separators=(",", ":"))
        _write_lines(path, lines)
        report = verify_path(tmp_path)
        assert report.status != "valid"
        assert report.first_invalid_record == 3

    def test_malformed_mid_segment_is_rejected(self, tmp_path):
        path = _write_chain(tmp_path, 6)
        lines = _read_lines(path)
        lines[3] = '{"event_id":"evt-broken", not valid json'
        _write_lines(path, lines)
        report = verify_path(tmp_path)
        assert report.status != "valid"
        assert report.first_invalid_record == 3


class TestStartupGateSegmentAware:
    def test_gate_passes_across_segment_boundary(self, tmp_path):
        # Reproduces the real-volume blocker at unit scale: a new daily file
        # re-anchors at genesis within the last-N hash-chain window.
        _write_chain(tmp_path, 5, date="2026-06-01")
        _write_chain(tmp_path, 4, date="2026-06-04")  # genesis re-anchor
        result = StartupValidator(_config(tmp_path)).check_audit_integrity()
        assert result.passed is True
        assert result.details["hash_chain_intact"] is True

    def test_gate_rejects_genesis_mid_file(self, tmp_path):
        path = _write_chain(tmp_path, 6)
        lines = _read_lines(path)
        mid = json.loads(lines[3])
        mid["prev_hash"] = GENESIS_HASH
        lines[3] = json.dumps(mid, separators=(",", ":"))
        _write_lines(path, lines)
        result = StartupValidator(_config(tmp_path)).check_audit_integrity()
        assert result.passed is False
        assert result.severity == "critical"


# ===========================================================================
# Scenario 5 — e2e preflight detection
# ===========================================================================


class TestPreflightDetection:
    def test_detects_marker_in_logs(self, tmp_path):
        path = _write_chain(tmp_path, 4)
        lines = _read_lines(path)
        last = json.loads(lines[-1])
        last["prev_hash"] = "0" * 64
        lines[-1] = json.dumps(last, separators=(",", ":"))
        _write_lines(path, lines)
        report = verify_path(tmp_path)
        msg = format_startup_failure(report)

        log_text = (
            "drnt-orchestrator | INFO Orchestrator starting\n"
            f"drnt-orchestrator | ERROR Startup validation CRITICAL failure "
            f"[audit_integrity]: {msg}\n"
        )
        assert is_audit_startup_failure(log_text) is True

    def test_clean_logs_not_flagged(self):
        assert is_audit_startup_failure("INFO all good, hub started\n") is False
        assert is_audit_startup_failure("") is False


# ===========================================================================
# CLI smoke tests
# ===========================================================================


class TestCli:
    def test_verify_exit_codes_and_report_json(self, tmp_path, capsys):
        _write_chain(tmp_path, 3)
        out = tmp_path / "report.json"
        rc = main(["verify", "--path", str(tmp_path), "--report-json", str(out)])
        assert rc == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["status"] == "valid"

    def test_repair_tail_cli(self, tmp_path):
        path = _write_chain(tmp_path, 6)
        lines = _read_lines(path)
        last = json.loads(lines[-1])
        last["prev_hash"] = "0" * 64
        lines[-1] = json.dumps(last, separators=(",", ":"))
        _write_lines(path, lines)

        manifest_out = tmp_path / "manifest.json"
        rc = main(["repair-tail", "--path", str(tmp_path), "--out", str(manifest_out)])
        assert rc == 0
        assert manifest_out.exists()
        assert verify_path(tmp_path).status == "valid"

    def test_repair_tail_cli_refuses_interior(self, tmp_path):
        path = _write_chain(tmp_path, 8)
        lines = _read_lines(path)
        mid = json.loads(lines[3])
        mid["payload"] = {"index": 999}
        lines[3] = json.dumps(mid, separators=(",", ":"))
        _write_lines(path, lines)
        rc = main(["repair-tail", "--path", str(tmp_path)])
        assert rc == 2  # RepairRefused
