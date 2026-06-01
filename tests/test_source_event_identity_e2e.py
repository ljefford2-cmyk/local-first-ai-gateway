"""§6B-1 / §6B-2 source-event identity — local-model bucket (live stack).

Insert D "local-model" tests: these drive a job through the real pipeline to
``proposal_ready`` / ``delivered`` (so they call the local model) and then verify
that re-sending the *same source event* with an *equivalent* intent dedups, and
with a *changed* intent returns the interim 409.

Run AFTER the free bucket passes
(``orchestrator/test_source_event.py`` + ``tests/test_source_event_identity.py``).

Requires:
    - The orchestrator image rebuilt from THIS branch (the running container must
      carry the §6B submit-path guard, not the pre-§6B accept-then-drop build).
    - Docker stack up; orchestrator reachable at DRNT_TEST_URL.
    - Local model available (``llama3.1:8b`` via drnt-ollama). Cost profile is
      local-only, no cloud — the profile of the prior truth run.

Usage:
    pytest tests/test_source_event_identity_e2e.py -v

The durable source-event store persists across runs, so each test uses a UNIQUE
client_source_event_id (uuid4) to avoid colliding with a prior run's mapping.

NOTE: not executed in the authoring session (no live §6B build was deployed).
The deterministic equivalents of these invariants — including the terminal +
restart durability case — are proven model-free in
``tests/test_source_event_identity.py``.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
import pytest

BASE_URL = os.environ.get("DRNT_TEST_URL", "http://localhost:8000")
TIMEOUT = int(os.environ.get("DRNT_TEST_TIMEOUT", "120"))
PROMPT = "What is the capital of France?"  # local-routing, reused across replays

pytestmark = pytest.mark.e2e


# ---------- helpers ----------

async def _get(path: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as c:
        return await c.get(path, params=params)


async def _post(path: str, json: dict) -> httpx.Response:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as c:
        return await c.post(path, json=json)


async def _ollama_available() -> bool:
    try:
        resp = await _get("/health")
        return resp.status_code == 200 and resp.json().get("ollama_status") in (
            "healthy", "degraded", "available",
        )
    except Exception:
        return False


def _source_event(event_id: str, raw_input: str = PROMPT, idem: str | None = None) -> dict:
    body = {
        "raw_input": raw_input,
        "input_modality": "text",
        "device": "phone",
        "client_source": "phone_app",
        "client_source_event_id": event_id,
        "client_timestamp": "2026-06-01T12:00:00Z",
    }
    if idem is not None:
        body["idempotency_key"] = idem
    return body


async def _wait_for_status(job_id: str, wanted: set[str], timeout: int = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await _get(f"/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        if data["status"] in wanted:
            return data
        await asyncio.sleep(1)
    raise AssertionError(f"job {job_id} did not reach {wanted} within {timeout}s")


# ---------- equivalent replay after proposal_ready ----------

@pytest.mark.asyncio
async def test_equivalent_replay_after_proposal_ready_dedups():
    if not await _ollama_available():
        pytest.skip("local model unavailable")

    event_id = f"e2e-pr-{uuid.uuid4()}"
    first = await _post("/jobs", _source_event(event_id, idem="k1"))
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]

    await _wait_for_status(job_id, {"proposal_ready", "delivered"})

    # Replay the same source event, normalized-equivalent intent, DIFFERENT idem
    # key → must dedup to the original job (no second model call).
    replay = await _post(
        "/jobs", _source_event(event_id, raw_input="  What is   the capital of France?  ", idem="k2-diff")
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["job_id"] == job_id


# ---------- equivalent replay after delivery ----------

@pytest.mark.asyncio
async def test_equivalent_replay_after_delivery_dedups():
    if not await _ollama_available():
        pytest.skip("local model unavailable")

    event_id = f"e2e-del-{uuid.uuid4()}"
    first = await _post("/jobs", _source_event(event_id, idem="k1"))
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]

    proposal = await _wait_for_status(job_id, {"proposal_ready", "delivered"})

    # Approve the proposal to reach delivered (if it is held for review).
    if proposal["status"] == "proposal_ready":
        prop = proposal["proposal"]
        review = await _post(
            f"/jobs/{job_id}/review",
            {
                "decision": "approve",
                "result_id": prop["result_id"],
                "response_hash": prop["response_hash"],
                "decision_idempotency_key": f"rev-{uuid.uuid4()}",
            },
        )
        assert review.status_code == 200, review.text
        await _wait_for_status(job_id, {"delivered"})

    # Replay after delivery → still dedups to the original terminal job.
    replay = await _post("/jobs", _source_event(event_id, idem="k3-diff"))
    assert replay.status_code == 202, replay.text
    assert replay.json()["job_id"] == job_id


# ---------- changed-intent replay → interim 409 ----------

@pytest.mark.asyncio
async def test_changed_intent_replay_returns_409():
    if not await _ollama_available():
        pytest.skip("local model unavailable")

    event_id = f"e2e-conflict-{uuid.uuid4()}"
    first = await _post("/jobs", _source_event(event_id, idem="k1"))
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]

    await _wait_for_status(job_id, {"proposal_ready", "delivered"})

    # Same source event, CHANGED intent → interim 409, never 202.
    conflict = await _post(
        "/jobs", _source_event(event_id, raw_input="What is the capital of Spain?", idem="k2-diff")
    )
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["source_event_conflict"] is True
    assert body["conflict_type"] == "intent_mismatch"
    assert body["original_job_id"] == job_id
    assert body["action_required"] == "reconfirmation_required"
    assert body["adr_compliance"] == "partial_until_reconfirmation_path_exists"
