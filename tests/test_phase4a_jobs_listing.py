"""Phase 4A.2.e GET /jobs listing tests.

Exercises the JobManager.list_jobs helper and the GET /jobs route per the
2026-04-26 plan amendment "Define jobs listing contract".

Coverage:
    - Query validation: required status, valid JobStatus, malformed since,
      limit bounds, default limit.
    - Response wrapper shape: items / next_cursor / count.
    - Filtering: status=proposal_ready returns only proposal_ready jobs.
    - Items are full JobStatusResponse objects with proposal populated for
      proposal_ready entries.
    - Newest-first ordering by UUIDv7 job_id descending.
    - Pagination via since={next_cursor} with no duplicates and exclusive
      boundary semantics for absent cursors.
    - Regression: GET /jobs/{job_id} still works and does not collide with
      GET /jobs.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import uuid_utils
from job_manager import JobManager
from models import Job, JobStatus
from test_helpers import MockAuditClient


# ---------- Test scaffolding ----------


def _uuid7() -> str:
    return str(uuid_utils.uuid7())


def _make_proposal_ready_job(
    *,
    job_id: Optional[str] = None,
    proposal_id: str = "prop-001",
    result_id: str = "res-001",
    response_hash: str = "hash-001",
    capability_id: str = "route.local",
    candidate_models: Optional[list[str]] = None,
    confidence: float = 0.91,
    result: str = "Paris.",
) -> Job:
    return Job(
        job_id=job_id or _uuid7(),
        raw_input="What is the capital of France?",
        input_modality="text",
        device="phone",
        status=JobStatus.proposal_ready.value,
        created_at="2026-04-26T12:00:00.000000Z",
        request_category="quick_lookup",
        routing_recommendation="local",
        candidate_models=candidate_models or ["llama3.1:8b"],
        governing_capability_id=capability_id,
        result=result,
        result_id=result_id,
        response_hash=response_hash,
        confidence=confidence,
        proposal_id=proposal_id,
    )


def _make_other_status_job(
    *,
    job_id: Optional[str] = None,
    status: str = JobStatus.submitted.value,
) -> Job:
    return Job(
        job_id=job_id or _uuid7(),
        raw_input="hi",
        input_modality="text",
        device="phone",
        status=status,
        created_at="2026-04-26T12:00:00.000000Z",
    )


def _make_manager() -> JobManager:
    return JobManager(audit_client=MockAuditClient())


def _seed_proposal_ready_jobs(jm: JobManager, n: int) -> list[Job]:
    """Seed n proposal-ready jobs with monotonically increasing job_ids."""
    jobs: list[Job] = []
    for i in range(n):
        j = _make_proposal_ready_job(
            proposal_id=f"prop-{i:03d}",
            result_id=f"res-{i:03d}",
            response_hash=f"hash-{i:03d}",
        )
        jm._jobs[j.job_id] = j
        jobs.append(j)
    return jobs


def _client_with_jm(jm: JobManager):
    """Bind the orchestrator app to a test JobManager and return a TestClient.

    Returns (client, restore) — call restore() to reset module-level state.
    """
    import main as main_module

    saved = main_module.job_manager
    main_module.job_manager = jm
    client = TestClient(main_module.app, raise_server_exceptions=False)

    def restore() -> None:
        main_module.job_manager = saved

    return client, restore


# ---------- 1. Query validation ----------


def test_missing_status_returns_422():
    jm = _make_manager()
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs")
        assert resp.status_code == 422
    finally:
        restore()


def test_invalid_status_returns_422():
    jm = _make_manager()
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "bogus_status"})
        assert resp.status_code == 422
    finally:
        restore()


def test_malformed_since_returns_422():
    jm = _make_manager()
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "since": "not-a-uuid"},
        )
        assert resp.status_code == 422
    finally:
        restore()


def test_limit_below_min_returns_422():
    jm = _make_manager()
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "0"},
        )
        assert resp.status_code == 422
    finally:
        restore()


def test_limit_above_max_returns_422():
    jm = _make_manager()
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "201"},
        )
        assert resp.status_code == 422
    finally:
        restore()


def test_limit_omitted_defaults_to_50():
    """Seeding 51 proposal_ready jobs with no limit returns 50, next_cursor set."""
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 51)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "proposal_ready"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 50
        assert len(data["items"]) == 50
        assert data["next_cursor"] is not None
    finally:
        restore()


# ---------- 2. Filtering & response shape ----------


def test_status_proposal_ready_returns_only_proposal_ready_jobs():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 3)
    for s in (
        JobStatus.submitted.value,
        JobStatus.classified.value,
        JobStatus.delivered.value,
        JobStatus.failed.value,
    ):
        j = _make_other_status_job(status=s)
        jm._jobs[j.job_id] = j

    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "proposal_ready"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 3
        assert all(item["status"] == "proposal_ready" for item in data["items"])
    finally:
        restore()


def test_response_wrapper_shape_includes_items_next_cursor_count():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 1)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "proposal_ready"})
        data = resp.json()
        assert "items" in data
        assert "next_cursor" in data
        assert "count" in data
    finally:
        restore()


def test_items_are_full_jobstatusresponse_objects():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 1)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "proposal_ready"})
        item = resp.json()["items"][0]
        for field in (
            "job_id",
            "status",
            "created_at",
            "request_category",
            "routing_recommendation",
            "result",
            "error",
            "proposal",
        ):
            assert field in item, f"missing JobStatusResponse field: {field}"
    finally:
        restore()


def test_proposal_populated_on_proposal_ready_items():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 2)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "proposal_ready"})
        data = resp.json()
        for item in data["items"]:
            assert item["proposal"] is not None
            assert item["proposal"]["job_id"] == item["job_id"]
            assert item["proposal"]["proposed_by"] == "llama3.1:8b"
            assert item["proposal"]["auto_accept_at"] is None
    finally:
        restore()


def test_count_equals_len_items():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 3)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get("/jobs", params={"status": "proposal_ready"})
        data = resp.json()
        assert data["count"] == len(data["items"])
    finally:
        restore()


# ---------- 3. Ordering & pagination ----------


def test_results_are_newest_first_by_job_id_descending():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 5)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "10"},
        )
        ids = [it["job_id"] for it in resp.json()["items"]]
        assert ids == sorted(ids, reverse=True)
    finally:
        restore()


def test_limit_truncates_page():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 5)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "2"},
        )
        data = resp.json()
        assert data["count"] == 2
        assert len(data["items"]) == 2
    finally:
        restore()


def test_next_cursor_is_last_id_when_more_remain():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 5)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "2"},
        )
        data = resp.json()
        last_id = data["items"][-1]["job_id"]
        assert data["next_cursor"] == last_id
    finally:
        restore()


def test_next_cursor_is_null_when_no_more_remain():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 2)
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "10"},
        )
        data = resp.json()
        assert data["next_cursor"] is None
    finally:
        restore()


def test_since_next_cursor_returns_next_page_older_than_cursor():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 5)
    client, restore = _client_with_jm(jm)
    try:
        page1 = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "2"},
        ).json()
        cursor = page1["next_cursor"]
        assert cursor is not None
        page2 = client.get(
            "/jobs",
            params={
                "status": "proposal_ready",
                "since": cursor,
                "limit": "2",
            },
        ).json()
        assert all(it["job_id"] < cursor for it in page2["items"])
    finally:
        restore()


def test_no_duplicates_across_pages_with_next_cursor_paging():
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 6)
    client, restore = _client_with_jm(jm)
    try:
        page1 = client.get(
            "/jobs",
            params={"status": "proposal_ready", "limit": "3"},
        ).json()
        page2 = client.get(
            "/jobs",
            params={
                "status": "proposal_ready",
                "since": page1["next_cursor"],
                "limit": "3",
            },
        ).json()
        ids1 = {it["job_id"] for it in page1["items"]}
        ids2 = {it["job_id"] for it in page2["items"]}
        assert len(ids1) == 3
        assert len(ids2) == 3
        assert ids1.isdisjoint(ids2)
    finally:
        restore()


def test_since_cursor_not_in_set_is_treated_as_exclusive_boundary():
    """A syntactically valid UUID not in the filtered set is a boundary, not 422."""
    jm = _make_manager()
    _seed_proposal_ready_jobs(jm, 3)
    # All-f UUID is syntactically valid hex and lexicographically > any UUIDv7
    # generated from an actual timestamp, so all seeded jobs are "older than"
    # this cursor.
    fake_cursor = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    client, restore = _client_with_jm(jm)
    try:
        resp = client.get(
            "/jobs",
            params={
                "status": "proposal_ready",
                "since": fake_cursor,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 3
    finally:
        restore()


# ---------- 4. Regression: GET /jobs/{job_id} still works ----------


def test_get_job_by_id_still_works_and_does_not_collide_with_get_jobs():
    jm = _make_manager()
    [job] = _seed_proposal_ready_jobs(jm, 1)
    client, restore = _client_with_jm(jm)
    try:
        # GET /jobs/{job_id}
        resp_one = client.get(f"/jobs/{job.job_id}")
        assert resp_one.status_code == 200
        body = resp_one.json()
        assert body["job_id"] == job.job_id
        assert body["status"] == "proposal_ready"

        # GET /jobs (list) on the same JobManager — should not collide
        resp_list = client.get("/jobs", params={"status": "proposal_ready"})
        assert resp_list.status_code == 200
        ids = [it["job_id"] for it in resp_list.json()["items"]]
        assert job.job_id in ids
    finally:
        restore()
