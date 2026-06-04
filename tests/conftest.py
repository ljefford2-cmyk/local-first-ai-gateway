"""Shared fixtures and marker registration for DRNT e2e tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx
import pytest

BASE_URL = os.environ.get("DRNT_TEST_URL", "http://localhost:8000")
TIMEOUT = int(os.environ.get("DRNT_TEST_TIMEOUT", "60"))
ORCH_CONTAINER = os.environ.get("DRNT_ORCH_CONTAINER", "drnt-orchestrator")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests against the live Docker stack")


async def _hub_reachable(timeout: int = 90) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=5) as c:
                r = await c.get("/health")
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


def _orchestrator_logs(tail: int = 160) -> str:
    """Best-effort fetch of orchestrator container logs (empty string on failure)."""
    for cmd in (
        ["docker", "logs", "--tail", str(tail), ORCH_CONTAINER],
        ["docker", "compose", "logs", "--tail", str(tail), "orchestrator"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        out = (r.stdout or "") + (r.stderr or "")
        if out.strip():
            return out
    return ""


def _audit_block_message() -> str | None:
    """If the orchestrator is down because audit_integrity failed at startup,
    return an actionable message; otherwise None.

    Reads the precise, fail-fast message the orchestrator already logged (Patch
    0C) so the operator is not left inferring an unrelated blocker for an hour.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
    try:
        from audit_integrity import AUDIT_FAILURE_MARKER, is_audit_startup_failure
    except Exception:
        return None

    logs = _orchestrator_logs()
    if not is_audit_startup_failure(logs):
        return None

    detail = next(
        (ln.strip() for ln in reversed(logs.splitlines()) if AUDIT_FAILURE_MARKER in ln),
        AUDIT_FAILURE_MARKER,
    )
    return (
        "E2E blocked by audit_integrity startup failure.\n"
        f"  {detail}\n"
        "Run the audit verifier/repair before boundary tests. "
        "See docs/AUDIT-RECOVERY.md."
    )


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def http_timeout():
    return TIMEOUT


@pytest.fixture(scope="session", autouse=True)
def stack_health_gate():
    """Gate the session on orchestrator reachability.

    Preflight: if the orchestrator is not immediately up, check whether the
    cause is an audit_integrity startup failure and, if so, fail fast with an
    actionable message rather than waiting out the full grace period only to
    skip with a vague "not reachable". Otherwise fall back to the normal wait.
    """
    # Fast path — already healthy.
    if asyncio.run(_hub_reachable(timeout=5)):
        return

    # Not up yet: is this the known audit_integrity blocker? Fail fast & loud.
    msg = _audit_block_message()
    if msg:
        pytest.fail(msg, pytrace=False)

    # Otherwise allow the normal grace period (slow start, image pull, etc.).
    if asyncio.run(_hub_reachable(timeout=90)):
        return

    # Still down — re-check (it may have crash-looped during the wait), else skip.
    msg = _audit_block_message()
    if msg:
        pytest.fail(msg, pytrace=False)
    pytest.skip("Orchestrator not reachable at " + BASE_URL)


@pytest.fixture(scope="session")
def async_client():
    """Reusable async HTTP client for the session."""
    client = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)
    yield client
    asyncio.run(client.aclose())
