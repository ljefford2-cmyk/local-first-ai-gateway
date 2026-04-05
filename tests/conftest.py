"""Shared fixtures and marker registration for DRNT e2e tests."""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest

BASE_URL = os.environ.get("DRNT_TEST_URL", "http://localhost:8000")
TIMEOUT = int(os.environ.get("DRNT_TEST_TIMEOUT", "60"))


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


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def http_timeout():
    return TIMEOUT


@pytest.fixture(scope="session", autouse=True)
def stack_health_gate():
    """Skip the entire session if orchestrator is unreachable."""
    if not asyncio.run(_hub_reachable(timeout=90)):
        pytest.skip("Orchestrator not reachable at " + BASE_URL)


@pytest.fixture(scope="session")
def async_client():
    """Reusable async HTTP client for the session."""
    client = httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT)
    yield client
    asyncio.run(client.aclose())
