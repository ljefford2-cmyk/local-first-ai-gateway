"""Phase 6D unit tests — Startup Validation Suite (Items 8-10).

Target: 18+ tests covering sandbox environment validation, egress posture
verification, audit log integrity checks, and hub integration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

from startup_validator import (
    CheckResult,
    HubConfig,
    StartupValidationError,
    StartupValidationReport,
    StartupValidator,
    _compute_hash,
    _GENESIS_HASH,
)
from events import (
    event_hub_startup_validated,
    event_hub_startup_blocked,
)
from test_helpers import MockAuditClient

# UUIDv7 pattern for event validation
UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_dir: str, **overrides) -> HubConfig:
    """Build a HubConfig rooted in a temp directory with all paths valid."""
    docker_sock = os.path.join(tmp_dir, "docker.sock")
    sandbox_dir = os.path.join(tmp_dir, "workers")
    audit_dir = os.path.join(tmp_dir, "audit")
    egress_state_dir = os.path.join(tmp_dir, "egress_state")
    seccomp_path = os.path.join(tmp_dir, "seccomp.json")
    egress_path = os.path.join(tmp_dir, "egress.json")

    # Create directories
    os.makedirs(sandbox_dir, exist_ok=True)
    os.makedirs(audit_dir, exist_ok=True)
    os.makedirs(egress_state_dir, exist_ok=True)

    # Create docker socket file (simulated)
    open(docker_sock, "w").close()

    # Create valid seccomp profile
    with open(seccomp_path, "w") as f:
        json.dump({"defaultAction": "SCMP_ACT_ERRNO"}, f)

    # Create valid egress config
    with open(egress_path, "w") as f:
        json.dump({
            "routes": [
                {
                    "route_id": "ollama-local",
                    "provider": "ollama",
                    "endpoint_url": "http://ollama:11434/api/generate",
                    "allowed_capabilities": ["route.local"],
                    "enabled": True,
                },
            ],
        }, f)

    kwargs = dict(
        docker_socket_path=docker_sock,
        worker_base_image="drnt-worker:latest",
        sandbox_base_dir=sandbox_dir,
        seccomp_profile_path=seccomp_path,
        egress_config_path=egress_path,
        ollama_url="http://127.0.0.1:99999",  # unreachable by default
        audit_log_dir=audit_dir,
        hash_chain_check_count=100,
        egress_state_dir=egress_state_dir,
    )
    kwargs.update(overrides)
    return HubConfig(**kwargs)


def _write_audit_log(audit_dir: str, events: list[dict], date: str = "2026-04-03"):
    """Write a valid hash-chained JSONL audit log file."""
    path = os.path.join(audit_dir, f"drnt-audit-{date}.jsonl")
    prev_hash = _GENESIS_HASH
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            evt["prev_hash"] = prev_hash
            line = json.dumps(evt, separators=(",", ":"))
            f.write(line + "\n")
            prev_hash = _compute_hash(line)
    return path


def _make_audit_events(count: int) -> list[dict]:
    """Generate N minimal audit events."""
    events = []
    for i in range(count):
        events.append({
            "event_id": f"evt-{i:04d}",
            "sequence": i,
            "event_type": "system.startup" if i == 0 else "test.event",
            "payload": {"index": i},
        })
    return events


def _assert_valid_envelope(evt, *, event_type, source):
    assert evt["schema_version"] == "2.0"
    assert UUID_V7_RE.match(evt["source_event_id"])
    assert isinstance(evt["timestamp"], str) and len(evt["timestamp"]) > 0
    assert evt["event_type"] == event_type
    assert evt["source"] == source
    assert isinstance(evt["payload"], dict)


# =============================================================================
# Item 8 Tests: Sandbox Environment (5+)
# =============================================================================


class TestSandboxEnvironment:
    """Item 8: Sandbox environment validation."""

    def test_docker_socket_accessible_passes(self):
        """Docker socket exists → sandbox check passes (sub-check)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            # Mock _check_base_image to avoid calling docker CLI
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.passed is True
            assert result.check_name == "sandbox_environment"
            assert result.details["docker_socket_accessible"] is True

    def test_docker_socket_missing_critical_fail(self):
        """Docker socket missing → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, docker_socket_path="/nonexistent/docker.sock")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["docker_socket_accessible"] is False
            assert "Docker socket" in result.message

    def test_base_image_exists_passes(self):
        """Base image exists → sandbox check passes (sub-check)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.details["base_image_exists"] is True

    def test_base_image_missing_critical_fail(self):
        """Base image missing → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: False
            result = validator.check_sandbox_environment()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["base_image_exists"] is False
            assert "Base image" in result.message

    def test_sandbox_directory_writable_passes(self):
        """Sandbox base directory exists and is writable → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.details["sandbox_dir_writable"] is True

    def test_sandbox_directory_missing_critical_fail(self):
        """Sandbox base directory missing → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, sandbox_base_dir="/nonexistent/sandbox")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["sandbox_dir_writable"] is False

    def test_seccomp_profile_valid_passes(self):
        """Valid seccomp JSON profile → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.details["seccomp_profile_valid"] is True

    def test_seccomp_profile_invalid_json_fails(self):
        """Invalid JSON in seccomp profile → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            # Overwrite with invalid JSON
            with open(config.seccomp_profile_path, "w") as f:
                f.write("{not valid json")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            result = validator.check_sandbox_environment()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["seccomp_profile_valid"] is False


# =============================================================================
# Item 9 Tests: Egress Posture (4+)
# =============================================================================


class TestEgressPosture:
    """Item 9: Egress posture validation."""

    @pytest.mark.asyncio
    async def test_egress_policy_loaded_passes(self):
        """Egress policy file exists and has routes → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            # Ollama won't be reachable, but that's just a warning
            result = await validator.check_egress_posture()
            assert result.details["egress_policy_loaded"] is True

    @pytest.mark.asyncio
    async def test_default_deny_active_passes(self):
        """Default-deny posture active (no overrides) → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            assert result.details["default_deny_active"] is True

    @pytest.mark.asyncio
    async def test_default_deny_overridden_critical_fail(self):
        """Default-deny overridden via config flag → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            # Overwrite egress config with default_deny: false
            with open(config.egress_config_path, "w") as f:
                json.dump({
                    "default_deny": False,
                    "routes": [{"route_id": "test", "allowed_capabilities": ["route.local"]}],
                }, f)
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["default_deny_active"] is False
            assert "overridden" in result.message.lower()

    @pytest.mark.asyncio
    async def test_default_deny_overridden_allow_all_critical_fail(self):
        """allow_all flag in egress config → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            with open(config.egress_config_path, "w") as f:
                json.dump({
                    "allow_all": True,
                    "routes": [{"route_id": "test", "allowed_capabilities": ["route.local"]}],
                }, f)
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            assert result.passed is False
            assert result.severity == "critical"

    @pytest.mark.asyncio
    async def test_default_deny_overridden_wildcard_critical_fail(self):
        """Wildcard capability in route → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            with open(config.egress_config_path, "w") as f:
                json.dump({
                    "routes": [{"route_id": "test", "allowed_capabilities": ["*"]}],
                }, f)
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            assert result.passed is False
            assert result.severity == "critical"

    @pytest.mark.asyncio
    async def test_ollama_unreachable_warning_not_critical(self):
        """Ollama unreachable → warning (hub can still start)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, ollama_url="http://127.0.0.1:99999")
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            # Ollama is unreachable, but this is only a warning
            assert result.details["ollama_reachable"] is False
            assert result.severity == "warning"
            assert "ollama_unreachable" in result.details.get("warnings", [])

    @pytest.mark.asyncio
    async def test_egress_policy_missing_critical_fail(self):
        """Egress config file missing → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, egress_config_path="/nonexistent/egress.json")
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["egress_policy_loaded"] is False

    @pytest.mark.asyncio
    async def test_stale_egress_state_detected(self):
        """Stale egress proxy state files → warning."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            # Create a stale .pid file
            stale = os.path.join(config.egress_state_dir, "proxy.pid")
            with open(stale, "w") as f:
                f.write("12345")
            validator = StartupValidator(config)
            result = await validator.check_egress_posture()
            assert result.details["no_stale_egress_state"] is False
            assert "stale_egress_state" in result.details.get("warnings", [])


# =============================================================================
# Item 10 Tests: Audit Log Integrity (5+)
# =============================================================================


class TestAuditIntegrity:
    """Item 10: Audit log integrity validation."""

    def test_audit_dir_writable_passes(self):
        """Audit log directory exists and is writable → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            result = validator.check_audit_integrity()
            assert result.details["audit_dir_writable"] is True

    def test_audit_dir_not_writable_critical_fail(self):
        """Audit log directory not writable → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, audit_log_dir="/nonexistent/audit")
            validator = StartupValidator(config)
            result = validator.check_audit_integrity()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["audit_dir_writable"] is False
            assert "not writable" in result.message.lower()

    def test_hash_chain_intact_passes(self):
        """Hash chain integrity verified → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            events = _make_audit_events(10)
            _write_audit_log(config.audit_log_dir, events)
            validator = StartupValidator(config)
            result = validator.check_audit_integrity()
            assert result.passed is True
            assert result.details["hash_chain_intact"] is True
            assert result.details["first_run"] is False

    def test_hash_chain_broken_critical_fail(self):
        """Hash chain broken (tampered event) → critical failure."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            events = _make_audit_events(5)
            path = _write_audit_log(config.audit_log_dir, events)

            # Tamper with the 3rd line (index 2) — change payload
            with open(path, "r") as f:
                lines = f.readlines()
            tampered = json.loads(lines[2])
            tampered["payload"]["index"] = 999  # tamper
            lines[2] = json.dumps(tampered, separators=(",", ":")) + "\n"
            with open(path, "w") as f:
                f.writelines(lines)

            validator = StartupValidator(config)
            result = validator.check_audit_integrity()
            assert result.passed is False
            assert result.severity == "critical"
            assert result.details["hash_chain_intact"] is False
            assert "hash chain broken" in result.message.lower()

    def test_empty_log_first_run_passes(self):
        """Empty audit log (first run) → initialize and pass."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            # No log files written — first run
            validator = StartupValidator(config)
            result = validator.check_audit_integrity()
            assert result.passed is True
            assert result.details["first_run"] is True
            assert "first run" in result.message.lower()

    def test_append_only_unsupported_passes_with_warning(self):
        """Append-only check on unsupported platform → pass with warning."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            events = _make_audit_events(3)
            _write_audit_log(config.audit_log_dir, events)
            validator = StartupValidator(config)
            # Force unsupported (mock _check_append_only)
            validator._check_append_only = lambda files: (False, "unsupported")
            result = validator.check_audit_integrity()
            assert result.passed is True
            assert "append_only_unsupported" in result.details.get("warnings", [])

    def test_hash_chain_check_bounded(self):
        """Hash chain check only verifies last N events (bounded)."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, hash_chain_check_count=5)
            events = _make_audit_events(20)
            _write_audit_log(config.audit_log_dir, events)
            validator = StartupValidator(config)
            result = validator.check_audit_integrity()
            assert result.passed is True
            assert result.details["hash_chain_intact"] is True


# =============================================================================
# Hub Integration Tests (4+)
# =============================================================================


class TestHubIntegration:
    """Hub integration: startup gating and audit event emission."""

    @pytest.mark.asyncio
    async def test_all_checks_pass_hub_starts(self):
        """All checks pass → hub_start_permitted is True."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            report = await validator.validate_all()
            assert report.hub_start_permitted is True
            assert len(report.critical_failures) == 0
            assert len(report.checks) == 3

    @pytest.mark.asyncio
    async def test_critical_failure_blocks_hub_start(self):
        """Any critical check fails → hub does not start."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, docker_socket_path="/nonexistent/docker.sock")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: False
            report = await validator.validate_all()
            assert report.hub_start_permitted is False
            assert "sandbox_environment" in report.critical_failures

    @pytest.mark.asyncio
    async def test_startup_blocked_event_emitted(self):
        """When hub is blocked, hub.startup_blocked event is produced."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, docker_socket_path="/nonexistent/docker.sock")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: False
            report = await validator.validate_all()

            assert not report.hub_start_permitted

            # Build the blocked event
            evt = event_hub_startup_blocked(
                blocking_checks=report.critical_failures,
                message=f"Hub startup blocked: {', '.join(report.critical_failures)} failed validation",
            )
            _assert_valid_envelope(evt, event_type="hub.startup_blocked", source="orchestrator")
            assert evt["payload"]["blocking_checks"] == report.critical_failures
            assert "sandbox_environment" in evt["payload"]["message"]

    @pytest.mark.asyncio
    async def test_startup_validated_event_emitted(self):
        """When hub passes, hub.startup_validated event is produced."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            report = await validator.validate_all()

            assert report.hub_start_permitted is True

            checks_passed = sum(1 for c in report.checks if c.passed)
            checks_failed = sum(1 for c in report.checks if not c.passed)
            evt = event_hub_startup_validated(
                hub_start_permitted=True,
                checks_passed=checks_passed,
                checks_failed=checks_failed,
                critical_failures=[],
                warnings=report.warnings,
            )
            _assert_valid_envelope(evt, event_type="hub.startup_validated", source="orchestrator")
            assert evt["payload"]["hub_start_permitted"] is True
            assert evt["payload"]["checks_passed"] == checks_passed

    @pytest.mark.asyncio
    async def test_warning_only_hub_starts(self):
        """Warning-only failures → hub starts with warnings logged."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, ollama_url="http://127.0.0.1:99999")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            report = await validator.validate_all()
            # Hub should start — Ollama unreachable is only a warning
            assert report.hub_start_permitted is True
            assert len(report.warnings) > 0
            assert "egress_posture" in report.warnings

    @pytest.mark.asyncio
    async def test_startup_validation_error_raised(self):
        """StartupValidationError carries the report and descriptive message."""
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp, docker_socket_path="/nonexistent/docker.sock")
            validator = StartupValidator(config)
            validator._check_base_image = lambda: False
            report = await validator.validate_all()
            err = StartupValidationError(report)
            assert err.report is report
            assert "sandbox_environment" in str(err)

    @pytest.mark.asyncio
    async def test_startup_event_is_first_audit_event(self):
        """Startup validation event should be the first event logged in a session."""
        audit = MockAuditClient()
        with tempfile.TemporaryDirectory() as tmp:
            config = _make_config(tmp)
            validator = StartupValidator(config)
            validator._check_base_image = lambda: True
            report = await validator.validate_all()

            assert report.hub_start_permitted is True

            # Emit the startup_validated event first (as hub would)
            checks_passed = sum(1 for c in report.checks if c.passed)
            checks_failed = sum(1 for c in report.checks if not c.passed)
            evt = event_hub_startup_validated(
                hub_start_permitted=True,
                checks_passed=checks_passed,
                checks_failed=checks_failed,
                critical_failures=[],
                warnings=report.warnings,
            )
            await audit.emit_durable(evt)

            # Verify it's the first event
            assert len(audit.events) == 1
            assert audit.events[0]["event_type"] == "hub.startup_validated"


# =============================================================================
# Dataclass and Report Tests (2+)
# =============================================================================


class TestDataclasses:
    """CheckResult and StartupValidationReport structure tests."""

    def test_check_result_fields(self):
        """CheckResult has all required fields."""
        cr = CheckResult(
            check_name="sandbox_environment",
            passed=True,
            severity="critical",
            message="All good",
            details={"docker_socket_accessible": True},
            checked_at="2026-04-03T00:00:00.000000Z",
        )
        assert cr.check_name == "sandbox_environment"
        assert cr.passed is True
        assert cr.severity == "critical"
        assert isinstance(cr.details, dict)
        assert cr.checked_at.endswith("Z")

    def test_report_aggregation(self):
        """StartupValidationReport correctly aggregates checks."""
        checks = [
            CheckResult("sandbox_environment", True, "critical", "OK", {}, _now_iso()),
            CheckResult("egress_posture", False, "warning", "Warn", {}, _now_iso()),
            CheckResult("audit_integrity", False, "critical", "Fail", {}, _now_iso()),
        ]
        critical = [c.check_name for c in checks if not c.passed and c.severity == "critical"]
        warnings = [c.check_name for c in checks if not c.passed and c.severity == "warning"]
        report = StartupValidationReport(
            hub_start_permitted=len(critical) == 0,
            checks=checks,
            critical_failures=critical,
            warnings=warnings,
            validated_at=_now_iso(),
        )
        assert report.hub_start_permitted is False
        assert report.critical_failures == ["audit_integrity"]
        assert report.warnings == ["egress_posture"]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
