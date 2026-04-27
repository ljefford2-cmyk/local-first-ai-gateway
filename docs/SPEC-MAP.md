# DRNT Spec-to-Implementation Map

Quick reference linking each specification to its code, config, and tests.
For per-claim detail, see [STATUS.md](../STATUS.md).

## Spec-to-Implementation Table

| Spec | Service | Primary Modules | Config Files | Test Files | Notes |
|------|---------|----------------|-------------|------------|-------|
| **1 -- Audit/Event Schema** | `audit-log-writer` | `src/log_writer.py`, `src/hash_chain.py`, `src/event_validator.py`, `src/file_manager.py`, `src/models.py` | None (env vars only) | `audit-log-writer/tests/test_be_ordering.py` | `scripts/drnt_audit_verify.py` is a standalone chain verification CLI |
| **2 -- Capability Model (WAL → Permissions)** | `orchestrator` | `job_manager.py`, `classifier.py`, `main.py`, `models.py`, `startup.py` | `config/capabilities.json` | `tests/test_integration_e2e.py`, `orchestrator/test_startup.py` | `main.py` is the FastAPI entrypoint; startup reconciles capability state |
| **3 -- Context Boundary Specification** | `orchestrator` | `context_packager.py` | `config/sensitivity.json` | `orchestrator/test_context_packager.py` | Strips/generalizes PII before dispatch |
| **4 -- Egress Policy Binding** | `egress-gateway` + `orchestrator` | **egress-gateway:** `registry.py`, `rate_limiter.py`, `main.py`, `providers/anthropic.py`, `providers/openai.py`, `providers/google.py`, `providers/ollama.py` | `config/egress.json` | `tests/test_phase6c.py`, `tests/test_phase6d.py`, `tests/test_durable_egress_audit.py` | **orchestrator-side:** `egress_proxy.py`, `egress_rate_limiter.py`, `egress_events.py` |
| **5 -- Override Semantics** | `orchestrator` | `permission_checker.py`, `demotion_engine.py`, `promotion_monitor.py`, `override_types.py`, `capability_state.py`, `capability_registry.py` | `config/capabilities.json` (`action_policies`, `promotion_criteria` sections) | `tests/test_phase5a.py` -- `test_phase5e.py`, `orchestrator/test_permission_checker.py`, `orchestrator/test_demotion_engine.py`, `orchestrator/test_promotion_monitor.py`, `orchestrator/test_capability_registry.py`, `orchestrator/test_banked_items.py` | Largest spec by test count; WAL levels gate autonomy |
| **6 -- Silo Runtime Security** | `orchestrator` + `worker-proxy` + `worker` | **orchestrator:** `runtime_manifest.py`, `manifest_validator.py`, `blueprint_engine.py`, `sandbox_blueprint.py`, `egress_proxy.py`, `worker_lifecycle.py`, `worker_context.py`, `worker_executor.py`, `startup_validator.py` | `config/seccomp-default.json` | `tests/test_phase6a.py` -- `test_phase6e.py`, `tests/test_worker_executor.py`, `tests/test_worker_proxy.py` | **worker-proxy:** `worker-proxy/main.py`, `worker-proxy/models.py`; **worker:** `worker/worker_agent.py`, `worker/Dockerfile`; test_phase6c/6d shared with Spec 4 |
| **7 -- Signal Chain Resilience** | `orchestrator` | `events.py`, `idempotency_store.py`, `connectivity_monitor.py`, `stale_recovery.py`, `decay_evaluator.py`, `hub_state.py` | None (behavioral, not config-driven) | `tests/test_spec7a_events.py` -- `test_spec7f_hub_state.py` | Six sub-specs (7A--7F), each with its own test file |
| **8 -- Managed Build Workflow** | — (process spec) | `docs/SPEC-8-MANAGED-BUILD-WORKFLOW.md`, `docs/plans/`, `docs/reports/` | None | None | Governs development workflow; see spec document |

## Cross-Cutting

| Module | Location | Spans Specs | Purpose |
|--------|----------|-------------|---------|
| `audit_client.py` | `orchestrator/` | 1--7 | Client used by orchestrator to send events to the audit log writer over Unix socket |
| `admin_routes.py` | `orchestrator/` | 2, 5 | HTTP API for capability management, override submission, and status queries |
| `pipeline_evaluator.py` | `orchestrator/` | 2, 5 | Evaluates pipeline outcomes for capability scoring |
| `test_helpers.py` | `orchestrator/` | -- | Shared test fixtures and utilities |
| `test_admin_routes.py` | `orchestrator/` | 2, 5 | Tests for admin HTTP endpoints (18 tests) |
| `docker-compose.yml` | project root | All | Service wiring, network topology (`drnt-internal`, `drnt-external`), volume mounts |

## Test Coverage Summary

| Spec | Test Files |
|------|-----------|
| 1 -- Audit/Event Schema | 1 |
| 2 -- Capability Model (WAL → Permissions) | 2 |
| 3 -- Context Boundary Specification | 1 |
| 4 -- Egress Policy Binding | 3 |
| 5 -- Override Semantics | 10 |
| 6 -- Silo Runtime Security | 6 |
| 7 -- Signal Chain Resilience | 6 |
| Cross-cutting (admin routes) | 1 |
| Phase 4A -- Mobile Agent Command Harness (Backend Contract) | 4 |
| **Total (deduplicated)** | **32** |

Note: `test_phase6c.py` and `test_phase6d.py` are shared between Specs 4 and 6.
Per-spec totals count these tests under both specs; the deduplicated total counts each test once.

---

## Phase 4A -- Mobile Agent Command Harness (Backend Contract)

Phase 4A.2 introduces the gateway-side Agent Proposal / Agent Inbox contract surface: a mobile-origin job is held for human review, surfaces as `proposal_ready`, is enumerable through a paginated inbox endpoint, and is resolved through an idempotent review decision with first-writer guards against override. See [STATUS.md](../STATUS.md) §"Phase 4A — Mobile Agent Command Harness (Backend Contract)" for claim-level status with evidence and [`docs/plans/phase-4a-backend-contract.md`](plans/phase-4a-backend-contract.md) for the authoritative plan and amendments through Phase 4A.2.e.

### Endpoints

| Endpoint | Implementation | Tests |
|----------|----------------|-------|
| `POST /jobs/{job_id}/review` | `orchestrator/main.py` (`review_job` route), `orchestrator/job_manager.py` (`JobManager.review_job` and `_review_approve` / `_review_edit` / `_review_reject` / `_review_defer` / `_review_decline_to_act` branches; `_review_wrong_status_response`, `_review_stale_response`) | `tests/test_phase4a_review_endpoint.py` |
| `GET /jobs` | `orchestrator/main.py` (`list_jobs` route), `orchestrator/job_manager.py` (`JobManager.list_jobs`), `orchestrator/models.py` (`JobListResponse`) | `tests/test_phase4a_jobs_listing.py` |
| `GET /jobs/{job_id}` (proposal population) | `orchestrator/main.py` (`get_job` route), `orchestrator/job_manager.py` (`JobManager.get_proposal`) | `tests/test_phase4a_proposal_population.py` |

### Models

| Model / field | Implementation |
|---------------|----------------|
| `JobStatus.proposal_ready`, `JobStatus.closed_no_action` | `orchestrator/models.py` (`JobStatus`) |
| `ClientSource` (`phone_app`, `watch_app`) | `orchestrator/models.py` (`ClientSource`) |
| `Proposal` | `orchestrator/models.py` (`Proposal`) |
| `ReviewDecision` | `orchestrator/models.py` (`ReviewDecision`) |
| `ReviewRequest.modified_result` | `orchestrator/models.py` (`ReviewRequest`) |
| `JobStatusResponse.proposal` | `orchestrator/models.py` (`JobStatusResponse`) |
| `JobListResponse` (`items`, `next_cursor`, `count`) | `orchestrator/models.py` (`JobListResponse`) |
| `Job.review_decision` (review-side first-writer guard) | `orchestrator/models.py` (`Job`) |
| `Job.proposal_id`, `Job.response_hash`, `Job.confidence` (proposal lineage) | `orchestrator/models.py` (`Job`) |

### `JobManager` behavior

| Behavior | Implementation |
|----------|----------------|
| Proposal derivation helper | `orchestrator/job_manager.py` (`JobManager.get_proposal`) |
| Hold-reason gating into `proposal_ready` | `orchestrator/job_manager.py` (`JobManager._run_pipeline`, `JobManager._derive_proposal_hold_reason`) |
| Review handler and per-decision branches | `orchestrator/job_manager.py` (`JobManager.review_job`, `_review_approve`, `_review_edit`, `_review_reject`, `_review_defer`, `_review_decline_to_act`) |
| Wrong-status / stale-decision 409 responses | `orchestrator/job_manager.py` (`JobManager._review_wrong_status_response`, `JobManager._review_stale_response`) |
| Listing helper (newest-first, cursor, paging, proposal population) | `orchestrator/job_manager.py` (`JobManager.list_jobs`) |
| Review/override first-writer serialization | `orchestrator/job_manager.py` (`JobManager.review_job` checks `override_type`; `JobManager.override_job` checks `review_decision` and sets `override_type` before durable emits on `proposal_ready` jobs) |

### Events

| Event | Builder | Emitter |
|-------|---------|---------|
| `job.proposal_ready` | `orchestrator/events.py` (`event_job_proposal_ready`) | `orchestrator/job_manager.py` (`JobManager._run_pipeline`) |
| `job.closed_no_action` | `orchestrator/events.py` (`event_job_closed_no_action`) | `orchestrator/job_manager.py` (`JobManager._review_decline_to_act`) |
| `human.reviewed` (Phase 4A `ReviewDecision` vocabulary) | `orchestrator/events.py` (`event_human_reviewed`) | `orchestrator/job_manager.py` (`JobManager._review_approve` / `_review_edit` / `_review_reject` / `_review_defer` / `_review_decline_to_act` preserve `approve | edit | reject | defer | decline_to_act` verbatim; legacy `override_job` and auto-accept emits keep their pre-existing strings) |

### Idempotency

| Surface | Implementation |
|---------|----------------|
| `review_idem:` namespace prefix | `orchestrator/idempotency_store.py` (`_REVIEW_KEY_PREFIX`) |
| Review outcome storage and lookup | `orchestrator/idempotency_store.py` (`IdempotencyStore.store_review_outcome`, `IdempotencyStore.get_review_outcome`, `ReviewIdempotencyRecord`) |
| Persistence (SQLite-backed `state` table; same-payload replay vs. different-payload 409) | `orchestrator/idempotency_store.py` (`IdempotencyStore._db_write_review`, `IdempotencyStore._load_from_db`) |
| Replay-before-stale-decision ordering and replay-response envelope | `orchestrator/job_manager.py` (`JobManager.review_job` evaluates idempotency replay first and stores the `(status_code, body)` envelope so replays are byte-identical without re-emitting durable events) |

### Tests

| Coverage | Test File |
|----------|-----------|
| Schema / contract (`JobStatus`, `ClientSource`, `Proposal`, `ReviewDecision`, `ReviewRequest`, `JobSubmitRequest`, `JobStatusResponse`, `extra="forbid"` rejection) | `tests/test_phase4a_backend_contract.py` |
| Proposal population (hold-reason gating, durable `job.proposal_ready` event, derivation helper, persistence roundtrip) | `tests/test_phase4a_proposal_population.py` |
| Review endpoint (per-decision behavior, wrong-status 409, stale-decision 409, `modified_result` 422, `review_idem:` replay, override serialization, `human.reviewed` vocabulary, `job.closed_no_action` payload, route-level surface) | `tests/test_phase4a_review_endpoint.py` |
| Jobs listing (required `status` filter, malformed/out-of-range 422s, newest-first ordering, exclusive UUIDv7 `since` cursor, paging without duplication, wrapper shape, proposal population in items, no collision with `GET /jobs/{job_id}`) | `tests/test_phase4a_jobs_listing.py` |

---

This table is a navigation aid. For claim-level status with evidence, see [STATUS.md](../STATUS.md). For security boundaries, see [THREAT-MODEL.md](../THREAT-MODEL.md). For worker execution details, see [docs/WORKER-EXECUTION.md](WORKER-EXECUTION.md).
