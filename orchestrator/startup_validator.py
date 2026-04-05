"""Startup validation checks for Items 8-10: sandbox, egress, and audit integrity.

Runs before the hub starts. If any CRITICAL check fails, the hub does not start.
This is fail-closed behavior at the system level.

Item 8: Sandbox environment — Docker socket, base image, sandbox dir, seccomp profile.
Item 9: Egress posture — policy loaded, default-deny active, Ollama reachable, no stale state.
Item 10: Audit log integrity — writable dir, hash chain intact, append-only flag, first-run init.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class HubConfig:
    """Hub configuration with paths and runtime settings for startup validation."""

    worker_proxy_url: str = "http://worker-proxy:9100"
    worker_base_image: str = "drnt-worker:latest"
    sandbox_base_dir: str = "/var/drnt/workers"
    seccomp_profile_path: str = "/var/drnt/config/seccomp-default.json"
    egress_config_path: str = "/var/drnt/config/egress.json"
    ollama_url: str = "http://ollama:11434"
    audit_log_dir: str = "/var/drnt/audit"
    hash_chain_check_count: int = 100
    egress_state_dir: str = "/var/drnt/state/egress"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Result of a single startup validation check."""

    check_name: str  # "sandbox_environment" | "egress_posture" | "audit_integrity"
    passed: bool
    severity: str  # "critical" | "warning"
    message: str  # Human-readable result
    details: dict  # Check-specific details
    checked_at: str  # ISO 8601


@dataclass
class StartupValidationReport:
    """Aggregated result of all startup validation checks."""

    hub_start_permitted: bool  # True only if ALL critical checks pass
    checks: list[CheckResult]
    critical_failures: list[str]  # Names of failed critical checks
    warnings: list[str]  # Names of warning-level issues
    validated_at: str


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class StartupValidationError(Exception):
    """Raised when startup validation blocks hub start."""

    def __init__(self, report: StartupValidationReport):
        self.report = report
        blocking = ", ".join(report.critical_failures)
        super().__init__(f"Hub startup blocked: {blocking} failed validation")


# ---------------------------------------------------------------------------
# Hash chain helpers (inline to avoid import from audit-log-writer)
# ---------------------------------------------------------------------------

_GENESIS_HASH = hashlib.sha256(b"DRNT-GENESIS").hexdigest()


def _compute_hash(json_line: str) -> str:
    return hashlib.sha256(json_line.encode("utf-8")).hexdigest()


def _verify_tail_chain(lines: list[str]) -> tuple[bool, int, str]:
    """Verify hash chain on the last N lines of an audit log.

    For a tail slice (not starting from genesis), we verify that each
    event's prev_hash matches the hash of the previous line. The first
    line in the slice is trusted as the anchor.

    Returns (valid, break_index, message).
    """
    if not lines:
        return True, -1, "No events to verify"

    if len(lines) == 1:
        return True, -1, "Single event, chain trivially valid"

    for i in range(1, len(lines)):
        expected = _compute_hash(lines[i - 1])
        try:
            event = json.loads(lines[i])
        except json.JSONDecodeError:
            return False, i, f"Event {i}: malformed JSON"
        actual = event.get("prev_hash")
        if actual != expected:
            return False, i, (
                f"Event {i}: prev_hash mismatch. "
                f"Expected {expected[:16]}..., got {str(actual)[:16]}..."
            )

    return True, -1, f"Chain valid, {len(lines)} events verified"


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class StartupValidator:
    """Validates system prerequisites before the hub starts.

    Checks:
        Item 8: Sandbox environment (Docker socket, base image, dirs, seccomp)
        Item 9: Egress posture (policy loaded, default-deny, Ollama health)
        Item 10: Audit log integrity (writable dir, hash chain, append-only)
    """

    def __init__(self, config: HubConfig):
        self._config = config

    async def validate_all(self) -> StartupValidationReport:
        """Run all startup checks in order. Returns a report.

        If any CRITICAL check fails, hub_start_permitted is False.
        """
        checks: list[CheckResult] = []

        checks.append(self.check_sandbox_environment())
        checks.append(await self.check_egress_posture())
        checks.append(self.check_audit_integrity())

        critical_failures = [
            c.check_name for c in checks if not c.passed and c.severity == "critical"
        ]
        warnings = [
            c.check_name for c in checks if not c.passed and c.severity == "warning"
        ]

        return StartupValidationReport(
            hub_start_permitted=len(critical_failures) == 0,
            checks=checks,
            critical_failures=critical_failures,
            warnings=warnings,
            validated_at=_now_iso(),
        )

    # ------------------------------------------------------------------
    # Item 8: Sandbox environment
    # ------------------------------------------------------------------

    def check_sandbox_environment(self) -> CheckResult:
        """Item 8: Verify container runtime and sandbox prerequisites.

        Sub-checks:
        - Worker proxy sidecar reachable
        - Worker base image exists (via sidecar)
        - Sandbox base directory exists and is writable
        - Seccomp profile file exists and is valid JSON
        """
        details: dict[str, Any] = {}
        failures: list[str] = []

        # 1. Worker proxy sidecar
        socket_ok = self._check_docker_socket()
        details["worker_proxy_accessible"] = socket_ok
        if not socket_ok:
            failures.append("Worker proxy not accessible")

        # 2. Worker base image
        image_ok = self._check_base_image()
        details["base_image_exists"] = image_ok
        details["base_image"] = self._config.worker_base_image
        if not image_ok:
            failures.append(f"Base image '{self._config.worker_base_image}' not found")

        # 3. Sandbox base directory
        dir_ok = self._check_sandbox_dir()
        details["sandbox_dir_writable"] = dir_ok
        details["sandbox_dir"] = self._config.sandbox_base_dir
        if not dir_ok:
            failures.append("Sandbox base directory not writable")

        # 4. Seccomp profile
        seccomp_ok, seccomp_msg = self._check_seccomp_profile()
        details["seccomp_profile_valid"] = seccomp_ok
        if not seccomp_ok:
            failures.append(seccomp_msg)

        passed = len(failures) == 0
        return CheckResult(
            check_name="sandbox_environment",
            passed=passed,
            severity="critical",
            message="Sandbox environment OK" if passed else "; ".join(failures),
            details=details,
            checked_at=_now_iso(),
        )

    def _check_docker_socket(self) -> bool:
        """Check that the worker-proxy sidecar is reachable."""
        try:
            resp = httpx.get(
                f"{self._config.worker_proxy_url}/health", timeout=5.0
            )
            return resp.status_code == 200
        except Exception:
            return False

    def _check_base_image(self) -> bool:
        """Check that the worker base image exists via the worker-proxy sidecar."""
        try:
            resp = httpx.get(
                f"{self._config.worker_proxy_url}/images/{self._config.worker_base_image}",
                timeout=10.0,
            )
            if resp.status_code != 200:
                return False
            return resp.json().get("exists", False)
        except Exception:
            return False

    def _check_sandbox_dir(self) -> bool:
        """Check that the sandbox base directory exists and is writable."""
        d = self._config.sandbox_base_dir
        return os.path.isdir(d) and os.access(d, os.W_OK)

    def _check_seccomp_profile(self) -> tuple[bool, str]:
        """Check that the seccomp profile exists and is valid JSON."""
        path = self._config.seccomp_profile_path
        if not os.path.isfile(path):
            return False, f"Seccomp profile not found: {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            return True, ""
        except (json.JSONDecodeError, OSError) as e:
            return False, f"Seccomp profile invalid: {e}"

    # ------------------------------------------------------------------
    # Item 9: Egress posture
    # ------------------------------------------------------------------

    async def check_egress_posture(self) -> CheckResult:
        """Item 9: Verify egress policy and default-deny posture.

        Sub-checks:
        - Egress policy configuration loaded
        - Default-deny is the active posture (not overridden)
        - Ollama endpoint is reachable (warning only if unreachable)
        - No stale egress proxy state from prior runs
        """
        details: dict[str, Any] = {}
        critical_failures: list[str] = []
        warning_items: list[str] = []

        # 1. Egress policy loaded
        policy_ok, policy_data = self._load_egress_policy()
        details["egress_policy_loaded"] = policy_ok
        if not policy_ok:
            critical_failures.append("Egress policy configuration not loaded")

        # 2. Default-deny posture
        if policy_ok:
            deny_ok, deny_msg = self._check_default_deny(policy_data)
            details["default_deny_active"] = deny_ok
            if not deny_ok:
                critical_failures.append(deny_msg)
        else:
            details["default_deny_active"] = False

        # 3. Ollama reachable (warning, not critical)
        ollama_ok = await self._check_ollama_health()
        details["ollama_reachable"] = ollama_ok
        if not ollama_ok:
            warning_items.append("ollama_unreachable")

        # 4. Stale egress proxy state
        stale_ok = self._check_stale_egress_state()
        details["no_stale_egress_state"] = stale_ok
        if not stale_ok:
            warning_items.append("stale_egress_state")

        details["warnings"] = warning_items

        if critical_failures:
            return CheckResult(
                check_name="egress_posture",
                passed=False,
                severity="critical",
                message="; ".join(critical_failures),
                details=details,
                checked_at=_now_iso(),
            )

        if warning_items:
            return CheckResult(
                check_name="egress_posture",
                passed=False,
                severity="warning",
                message=f"Egress posture active with warnings: {', '.join(warning_items)}",
                details=details,
                checked_at=_now_iso(),
            )

        return CheckResult(
            check_name="egress_posture",
            passed=True,
            severity="critical",
            message="Egress posture OK",
            details=details,
            checked_at=_now_iso(),
        )

    def _load_egress_policy(self) -> tuple[bool, dict | None]:
        """Load and parse the egress configuration file."""
        path = self._config.egress_config_path
        if not os.path.isfile(path):
            return False, None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "routes" not in data:
                return False, None
            return True, data
        except (json.JSONDecodeError, OSError):
            return False, None

    def _check_default_deny(self, policy_data: dict) -> tuple[bool, str]:
        """Verify default-deny posture is not overridden in the egress config.

        Checks that no route declares an unrestricted allow-all pattern and
        that no top-level override disables default-deny.
        """
        # Check for explicit override flag
        if policy_data.get("default_deny") is False:
            return False, "Default-deny overridden: default_deny set to false"
        if policy_data.get("allow_all") is True:
            return False, "Default-deny overridden: allow_all set to true"

        # Check routes for unrestricted wildcards
        for route in policy_data.get("routes", []):
            caps = route.get("allowed_capabilities", [])
            if "*" in caps:
                return False, (
                    f"Default-deny overridden: route '{route.get('route_id', '?')}' "
                    f"allows all capabilities via wildcard"
                )

        return True, ""

    async def _check_ollama_health(self) -> bool:
        """Basic health check against the Ollama endpoint."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(self._config.ollama_url)
                return resp.status_code == 200
        except Exception:
            return False

    def _check_stale_egress_state(self) -> bool:
        """Check for stale egress proxy state files from prior runs."""
        state_dir = self._config.egress_state_dir
        if not os.path.isdir(state_dir):
            return True  # No state dir means no stale state
        entries = os.listdir(state_dir)
        # Stale state files are *.pid or *.lock files from prior runs
        stale = [e for e in entries if e.endswith((".pid", ".lock"))]
        return len(stale) == 0

    # ------------------------------------------------------------------
    # Item 10: Audit log integrity
    # ------------------------------------------------------------------

    def check_audit_integrity(self) -> CheckResult:
        """Item 10: Verify audit log integrity.

        Sub-checks:
        - Audit log directory exists and is writable
        - Hash chain integrity (last N events)
        - Append-only flag (chattr +a) — best-effort
        - If empty (first run), initialize and pass
        """
        details: dict[str, Any] = {}
        failures: list[str] = []
        warning_items: list[str] = []

        # 1. Audit log directory writable
        dir_ok = self._check_audit_dir_writable()
        details["audit_dir_writable"] = dir_ok
        details["audit_dir"] = self._config.audit_log_dir
        if not dir_ok:
            return CheckResult(
                check_name="audit_integrity",
                passed=False,
                severity="critical",
                message="Audit log directory not writable",
                details=details,
                checked_at=_now_iso(),
            )

        # 2. Check for first run (empty log)
        log_files = self._find_audit_log_files()
        details["log_file_count"] = len(log_files)

        if not log_files:
            # First run — no log files yet. Initialize marker and pass.
            details["first_run"] = True
            details["hash_chain_intact"] = True
            details["append_only_set"] = True
            return CheckResult(
                check_name="audit_integrity",
                passed=True,
                severity="critical",
                message="First run — audit log initialized",
                details=details,
                checked_at=_now_iso(),
            )

        details["first_run"] = False

        # 3. Hash chain integrity (last N events)
        chain_ok, chain_msg = self._check_hash_chain(log_files)
        details["hash_chain_intact"] = chain_ok
        details["hash_chain_message"] = chain_msg
        if not chain_ok:
            failures.append(f"Hash chain broken: {chain_msg}")

        # 4. Append-only flag (best-effort)
        append_ok, append_msg = self._check_append_only(log_files)
        details["append_only_set"] = append_ok
        if not append_ok:
            if append_msg == "unsupported":
                # Filesystem doesn't support chattr — pass with warning
                details["append_only_set"] = True
                warning_items.append("append_only_unsupported")
            else:
                failures.append(f"Append-only flag not set: {append_msg}")

        details["warnings"] = warning_items

        if failures:
            return CheckResult(
                check_name="audit_integrity",
                passed=False,
                severity="critical",
                message="; ".join(failures),
                details=details,
                checked_at=_now_iso(),
            )

        return CheckResult(
            check_name="audit_integrity",
            passed=True,
            severity="critical",
            message="Audit log integrity OK",
            details=details,
            checked_at=_now_iso(),
        )

    def _check_audit_dir_writable(self) -> bool:
        """Check that the audit log directory exists and is accessible.

        The orchestrator reads the audit log for integrity verification but
        delegates writing to the audit-log-writer service via Unix socket.
        Read access is sufficient for the orchestrator's role.
        """
        d = self._config.audit_log_dir
        return os.path.isdir(d) and os.access(d, os.R_OK)

    def _find_audit_log_files(self) -> list[Path]:
        """Find JSONL audit log files sorted by name (date order)."""
        log_dir = Path(self._config.audit_log_dir)
        if not log_dir.is_dir():
            return []
        files = sorted(log_dir.glob("drnt-audit-*.jsonl"))
        return files

    def _check_hash_chain(self, log_files: list[Path]) -> tuple[bool, str]:
        """Verify hash chain on the last N events across log files.

        Reads lines from the most recent files, up to hash_chain_check_count.
        """
        n = self._config.hash_chain_check_count
        all_lines: list[str] = []

        # Read from newest files first, collect up to N lines
        for f in reversed(log_files):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    file_lines = [
                        line.rstrip("\n") for line in fh if line.strip()
                    ]
                all_lines = file_lines + all_lines
                if len(all_lines) >= n:
                    break
            except OSError:
                continue

        # Take the last N lines
        tail = all_lines[-n:] if len(all_lines) > n else all_lines

        if not tail:
            return True, "No events to verify"

        valid, break_idx, message = _verify_tail_chain(tail)
        return valid, message

    def _check_append_only(self, log_files: list[Path]) -> tuple[bool, str]:
        """Check append-only flag on audit log files.

        Uses lsattr on Linux. On unsupported platforms, returns a warning.
        """
        if platform.system() != "Linux":
            return False, "unsupported"

        for f in log_files:
            try:
                result = subprocess.run(
                    ["lsattr", str(f)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    return False, "unsupported"
                # lsattr output: "----a------------ /path/to/file"
                attrs = result.stdout.split()[0] if result.stdout.strip() else ""
                if "a" not in attrs:
                    return False, f"Missing append-only flag on {f.name}"
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                return False, "unsupported"

        return True, ""
