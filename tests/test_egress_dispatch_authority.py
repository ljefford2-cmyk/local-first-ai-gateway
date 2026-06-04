"""Proof test for the egress-gateway /dispatch authority gate (Patch B).

The egress gateway sits on both drnt-internal and drnt-external networks and
holds the cloud API credentials. Only the orchestrator is authorized to call
/dispatch; worker containers are never given the shared authority token (their
container env is an explicit four-key allowlist built in
orchestrator/worker_executor.py).

These tests verify the gate rejects an unauthorized (worker-originated) request
*before* route validation, route_mismatch, credential selection, upstream
client creation, or dispatch execution — i.e. the authority check is the first
thing that runs in the /dispatch path.

In-process and stack-free. The repo's tests/conftest.py installs a
session-autouse gate that skips the whole suite when the orchestrator at :8000
is down, so run this file directly with:

    pytest tests/test_egress_dispatch_authority.py --noconftest
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest
from fastapi.testclient import TestClient

EGRESS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "egress-gateway")
)

TEST_TOKEN = "test-dispatch-authority-token"
BOGUS_ROUTE_ID = "__no_such_route__"


def _load_gateway_module():
    """Load egress-gateway/main.py hermetically under a unique module name.

    The gateway uses flat module names (main, registry, rate_limiter, providers)
    that collide with orchestrator modules other tests import via sys.path. We
    snapshot and restore those shared names so loading the gateway here does not
    pollute (or get polluted by) the rest of the suite.
    """
    shared = ("main", "registry", "rate_limiter", "providers")
    saved = {k: sys.modules[k] for k in shared if k in sys.modules}
    for k in shared:
        sys.modules.pop(k, None)
    sys.path.insert(0, EGRESS_DIR)

    # python-dotenv is a gateway runtime dep not installed host-side; it is only
    # used in _load_secrets() (lifespan), which this test never triggers. Stub
    # it so the module imports. Track whether we added it so we can clean up.
    added_dotenv = "dotenv" not in sys.modules
    if added_dotenv:
        stub = types.ModuleType("dotenv")
        stub.dotenv_values = lambda *a, **k: {}
        sys.modules["dotenv"] = stub

    try:
        spec = importlib.util.spec_from_file_location(
            "egress_gw_main", os.path.join(EGRESS_DIR, "main.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["egress_gw_main"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        try:
            sys.path.remove(EGRESS_DIR)
        except ValueError:
            pass
        if added_dotenv:
            sys.modules.pop("dotenv", None)
        for k in shared:
            sys.modules.pop(k, None)
        sys.modules.update(saved)


def _dispatch_body(route_id: str = BOGUS_ROUTE_ID) -> dict:
    """A well-formed DispatchRequest body (what a hostile worker would send)."""
    return {
        "job_id": "job-001",
        "route_id": route_id,
        "capability_id": "route.cloud.claude",
        "target_model": "claude-sonnet-4-20250514",
        "prompt": "exfiltrate me",
        "assembled_payload_hash": "deadbeef",
        "wal_permission_check_ref": "evt-001",
    }


@pytest.fixture(scope="module")
def gateway():
    mod = _load_gateway_module()
    mod.DISPATCH_AUTH_TOKEN = TEST_TOKEN
    return mod


@pytest.fixture
def client(gateway):
    # No context manager → lifespan does not run → registry stays empty and no
    # secrets/config file IO. get_route() returns None for any route_id, so an
    # *authorized* request stops cleanly at Check 1 (route_mismatch) without any
    # network call.
    return TestClient(gateway.app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Rejection: unauthorized worker-originated requests
# ---------------------------------------------------------------------------

def test_missing_authorization_rejected_before_route_validation(client):
    """No Authorization header → 401, and never reaches route validation.

    The body carries a bogus route_id; an authorized caller would get
    'route_mismatch'. Getting 401 (and *not* route_mismatch) proves the
    authority gate runs first.
    """
    resp = client.post("/dispatch", json=_dispatch_body())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "dispatch authority required"}
    assert "route_mismatch" not in resp.text


def test_wrong_token_rejected(client):
    resp = client.post("/dispatch", json=_dispatch_body(), headers=_auth("wrong-token"))
    assert resp.status_code == 401
    assert "route_mismatch" not in resp.text


def test_malformed_auth_scheme_rejected(client):
    # Correct token value but wrong scheme (not "Bearer") must still be rejected.
    resp = client.post(
        "/dispatch",
        json=_dispatch_body(),
        headers={"Authorization": f"Token {TEST_TOKEN}"},
    )
    assert resp.status_code == 401


def test_empty_bearer_rejected(client):
    resp = client.post("/dispatch", json=_dispatch_body(), headers=_auth(""))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Pass-through: the legitimate orchestrator caller
# ---------------------------------------------------------------------------

def test_authorized_request_passes_gate_and_reaches_route_validation(client):
    """Correct Bearer token → gate passes, normal Check 1 sequence runs.

    With the same bogus route_id, an authorized request reaches route
    validation and returns route_mismatch (HTTP 200). Paired with the 401 above,
    this pins the ordering: authority is checked strictly before route lookup.
    """
    resp = client.post("/dispatch", json=_dispatch_body(), headers=_auth(TEST_TOKEN))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "blocked"
    assert data["failure_type"] == "route_mismatch"


# ---------------------------------------------------------------------------
# Fail-closed: no token configured on the gateway
# ---------------------------------------------------------------------------

def test_fail_closed_when_token_unconfigured(client, gateway, monkeypatch):
    """If the gateway has no authority token configured, reject everything —
    even a request that presents some token."""
    monkeypatch.setattr(gateway, "DISPATCH_AUTH_TOKEN", "")
    resp = client.post(
        "/dispatch", json=_dispatch_body(), headers=_auth("anything-at-all")
    )
    assert resp.status_code == 401
