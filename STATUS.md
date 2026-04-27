# DRNT Gateway — Claim-Status-Evidence Matrix

**Version:** v0.2.1
**Generated:** 2026-04-18
**Purpose:** Map every architectural claim to its actual implementation status and evidence. This document exists because the four-model adversarial review (April 2026) converged on a single finding: the governance language is more mature than the runtime, and the public narrative risks outrunning implementation completeness.

**Status definitions:**

- **Implemented** — Code exists, is wired into the runtime, and is exercised by tests.
- **Partial** — Code exists but is incomplete, has known V1 limitations, or is not fully wired.
- **Aspirational** — Described in specs or README but not yet built.

---

## Spec 1 — Audit/Event Schema

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 1.1 | SHA-256 hash chain links every event to its predecessor | Implemented | `audit-log-writer/hash_chain.py`: `compute_hash()`, `verify_chain()`. Genesis seed is `SHA-256("DRNT-GENESIS")`. Chain state recovered on startup via `file_manager.py:recover_chain_state()`. |
| 1.2 | UUIDv7 event IDs assigned by writer | Implemented | `audit-log-writer/log_writer.py:_commit_event()` generates `uuid7()` for `event_id`. |
| 1.3 | Envelope schema validation on inbound events | Implemented | `audit-log-writer/event_validator.py:validate_event()` — checks all required fields, UUIDv7 format, ISO 8601 timestamps, source/durability enums, payload type, WAL level range. |
| 1.4 | Source-event deduplication | Implemented | `audit-log-writer/log_writer.py` — `_seen_source_ids` set checked before commit. Returns `DuplicateResponse` on replay. |
| 1.5 | Daily JSONL file rotation | Implemented | `audit-log-writer/file_manager.py` — files named `drnt-audit-YYYY-MM-DD.jsonl`. Midnight rollover handled in `_commit_event()`. |
| 1.6 | Durable events use fsync | Implemented | `audit-log-writer/file_manager.py:append_line()` — `fsync=True` path calls `os.fsync()`. |
| 1.7 | Best-effort events use buffered writes | Implemented | `audit-log-writer/log_writer.py` — `_be_buffer` flushed on interval or before next durable event. |
| 1.8 | Unix domain socket IPC (length-prefixed JSON) | Implemented | `audit-log-writer/log_writer.py` (server), `orchestrator/audit_client.py` (client). 4-byte big-endian length prefix. |
| 1.9 | Chain state recovery on startup | Implemented | `audit-log-writer/file_manager.py:recover_chain_state()` — reads today's and yesterday's files to rebuild `prev_hash`, sequence counter, and dedup set. |
| 1.10 | Standalone chain verification tool | Implemented | `audit-log-writer/drnt_audit_verify.py` — CLI tool that recomputes every hash from genesis across one or more JSONL files. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 1.11 | Audit logs persist across container restarts | Implemented | `docker-compose.yml` — `audit-logs` named Docker volume mounted at `/var/drnt/audit`. |
| 1.12 | Audit log writer runs as isolated service | Implemented | `docker-compose.yml` — `drnt-audit-log-writer` service on `drnt-internal` network only. |

---

## Spec 2 — Capability Model (WAL → Permissions)

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 2.1 | Job lifecycle state machine (submitted → classified → dispatched → response_received → delivered) | Implemented | `orchestrator/models.py:JobStatus` enum. Transitions driven by `job_manager.py:_process_job()`. |
| 2.2 | HTTP API for job submit, status, health | Implemented | `orchestrator/main.py` — FastAPI routes: `POST /jobs`, `GET /jobs/{id}`, `GET /health`. |
| 2.3 | Ollama-based request classification | Implemented | `orchestrator/classifier.py:classify()` — sends raw input to local Ollama, returns `request_category` and `routing_recommendation`. |
| 2.4 | Cloud dispatch via egress gateway | Implemented | `orchestrator/job_manager.py` — HTTP POST to `EGRESS_GATEWAY_URL/dispatch` with model, prompt, route_id. |
| 2.5 | Local dispatch via Ollama | Implemented | `orchestrator/job_manager.py` + `classifier.py:generate_local_response()`. |
| 2.6 | Result artifacts written to disk | Implemented | `orchestrator/job_manager.py:_write_result()` — writes to `DRNT_RESULTS_DIR`. Docker volume `results-store` backs the path. |
| 2.7 | Async pipeline with bounded queue | Implemented | `orchestrator/job_manager.py` — `asyncio.Queue(maxsize=256)`, background `_worker_loop()`. |
| 2.8 | Capability registry loaded at startup | Implemented | `orchestrator/capability_registry.py` loaded from `config/capabilities.json`. Validated during `startup.py:run()`. |
| 2.9 | Capability state reconciliation at startup | Implemented | `orchestrator/startup.py:run()` — validates config, reconciles state file, checks Ollama model version, emits audit events. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 2.10 | Job state persists across restarts | **Implemented** | `job_manager.py` uses write-through SQLite cache via `persistence.py`. Non-terminal jobs loaded on startup. Tested in `test_job_persistence.py` (restart survival, terminal filtering, stale recovery). |
| 2.11 | Egress gateway actually dispatches to cloud providers | **Partial** | `egress-gateway/` service defined in `docker-compose.yml`. Provider adapter files exist (`anthropic.py`, `openai.py`, `google.py`, `ollama.py`). Orchestrator sends HTTP POST to egress gateway. Full round-trip depends on egress gateway service running and API keys configured. |

---

## Spec 3 — Context Boundary Specification

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 3.1 | Regex-based sensitivity scanning | Implemented | `orchestrator/context_packager.py:_scan()` — iterates all compiled patterns from `sensitivity.json`. |
| 3.2 | Strip action replaces matches with `[REDACTED:class]` | Implemented | `context_packager.py:_apply_transform()` — strip action path. |
| 3.3 | Generalize action (zip codes → 315XX, dates → year) | Implemented | `context_packager.py` — `_generalize_location()`, `_generalize_date()`. |
| 3.4 | Allowlist prevents false positives | Implemented | `context_packager.py:SensitivityConfig` — per-class `allowlist` set. `sensitivity.json` includes 70+ allowlisted terms. |
| 3.5 | Overlapping span merge (most restrictive wins) | Implemented | `context_packager.py:_merge_overlapping_spans()` — sorts by position and action precedence. |
| 3.6 | Context object persisted to disk | Implemented | `context_packager.py:package()` — writes JSON to `context_store_dir/{context_package_id}.json`. |
| 3.7 | Audit events emitted for packaging and strip details | Implemented | `context_packager.py:package()` — emits `context.packaged` and per-span `context.strip_detail` events. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 3.8 | Multi-field context assembly (multiple data sources) | **Partial** | `context_packager.py:package()` only handles a single `user_input` field. The `eligible_context_fields` list always has exactly one entry. Multi-source assembly (RAG, conversation history, document context) is not implemented. |
| 3.9 | Sensitivity regex handles adversarial evasion | **Partial** | Regex patterns in `sensitivity.json` are basic. No Unicode normalization, no homoglyph detection, no encoding-bypass resistance. The review flagged this as "sensitivity regex brittleness." |

---

## Spec 4 — Egress Policy Binding

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 4.1 | Provider routing configuration | Implemented | `config/egress.json` — four routes defined (Anthropic, OpenAI, Google, Ollama) with route IDs, endpoints, capabilities, auth, rate limits, health config. |
| 4.2 | Per-route auth method configuration | Implemented | `egress.json` — `bearer_token` for Anthropic/OpenAI, `api_key_param` for Google, `none` for Ollama. |
| 4.3 | Rate limiting per capability | Implemented | `orchestrator/egress_rate_limiter.py` — sliding-window RPM limiter. Wired into `EgressProxy.authorize()`. In-memory only. |
| 4.4 | Network isolation (dual Docker networks) | Implemented | `docker-compose.yml` — `drnt-internal` (service mesh) and `drnt-external` (cloud egress). Orchestrator on internal only. Egress gateway bridges both. |
| 4.5 | Secrets bind-mounted read-only | Implemented | `docker-compose.yml` — `./secrets:/var/drnt/secrets:ro` on egress-gateway service. `.env.example` tracked, `.env` gitignored. |
| 4.6 | Egress proxy allowlist enforcement | Implemented | `orchestrator/egress_proxy.py:authorize()` — five-rule evaluation: network_mode none → deny; Ollama → allow; blueprint + policy → allow; blueprint without policy → deny; default deny. |
| 4.7 | Egress audit event generation | Implemented | `orchestrator/egress_events.py` — `event_egress_authorized()`, `event_egress_denied()` with denial reasons. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 4.8 | Egress audit events persisted to durable log | **Implemented** | `egress_proxy.py:log_request()` routes egress audit events to the durable audit log via `audit_client.emit_durable()`. 14 dedicated tests in `test_durable_egress_audit.py` confirm the wiring. |
| 4.9 | Seccomp profile applied to worker containers | **Implemented** | `config/seccomp-default.json` exists and is passed into worker execution. Runtime enforcement is verified by `tests/test_e2e_v02.py::TestWorkerExecution::test_worker_seccomp_enforcement_blocks_disallowed_syscall`: the fixed-contract probe confirms `personality(0xFFFFFFFF)` is blocked with `errno=EPERM` inside a worker container while control syscall `getpid()` succeeds. Earlier contradictory diagnosis was resolved by failed-path result retention in commit `1073535` and probe infrastructure in commit `13a7abb`. Verification is scoped to the blocked-syscall assertion; full profile exhaustiveness is not claimed. See KI-1 for historical context. |

---

## Spec 5 — Override Semantics

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 5.1 | Cancel override (pre-delivery: fail job; post-delivery: revoke) | Implemented | `orchestrator/job_manager.py:override_job()` — cancel branch handles both pre-delivery fail and post-delivery revoke with audit events. |
| 5.2 | Redirect override (fail + spawn successor to new route) | Implemented | `job_manager.py:override_job()` — redirect branch + `_spawn_successor()`. |
| 5.3 | Modify override (replace delivered result) | Implemented | `job_manager.py:override_job()` — modify branch writes new result, emits `human.reviewed`. |
| 5.4 | Escalate override (spawn successor to higher-tier route) | Implemented | `job_manager.py:override_job()` — escalate branch + `_spawn_successor()`. |
| 5.5 | First-override-wins guard | Implemented | `job_manager.py:override_job()` — checks `job.override_type is not None`, returns no-op if already overridden. |
| 5.6 | WAL demotion on cancel/redirect (conditional) | Implemented | `job_manager.py` integrates `demotion_engine.py`. `override_types.py:CONDITIONAL_DEMOTION_TYPES`. |
| 5.7 | Sentinel failure suppresses demotion | Implemented | `override_types.py:is_sentinel_failure()` — `egress_config`, `egress_connectivity`, `worker_sandbox` IDs bypass demotion. |
| 5.8 | Auto-accept window for WAL-2+ delivered jobs | Implemented | `job_manager.py:_auto_accept_loop()` — polls every `AUTO_ACCEPT_POLL_INTERVAL` seconds, auto-accepts delivered WAL-2+ jobs after `AUTO_ACCEPT_WINDOW_SECONDS` (default 24h). |
| 5.9 | Permission checker (WAL-level gate before dispatch) | Implemented | `orchestrator/permission_checker.py` — checks capability WAL level, returns allow/block/hold decisions with audit events. |
| 5.10 | Demotion engine (3 failures in 24h trigger) | Implemented | `orchestrator/demotion_engine.py` — ring-buffer outcome tracking, configurable thresholds. |
| 5.11 | Promotion monitor (success-based WAL elevation) | Implemented | `orchestrator/promotion_monitor.py` — tracks consecutive successes, proposes promotions. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 5.12 | Override flow exercised end-to-end with real dispatch | **Partial** | Override logic is fully wired in `job_manager.py` and tested extensively (69 tests across `test_phase5a–5e.py`). However, the full override → re-dispatch → new model response cycle has not been demonstrated against a live provider. |

---

## Spec 6 — Silo Runtime Security

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 6.1 | Runtime manifest structure | Implemented | `orchestrator/runtime_manifest.py` — `RuntimeManifest`, `NetworkPolicy`, `ResourceLimits`, `SecurityPolicy`, `VolumeMount` dataclasses. Worker-type-to-min-WAL mapping. |
| 6.2 | Manifest validator | Implemented | `orchestrator/manifest_validator.py` — validates worker type, volume paths, network policy, resource limits, security policy, WAL level requirements. |
| 6.3 | Blueprint engine (manifest → sandbox config) | Implemented | `orchestrator/blueprint_engine.py` — generates `SandboxBlueprint` with Docker run arguments, mount specs, security options, resource constraints. |
| 6.4 | Egress proxy per-worker scoping | Implemented | `orchestrator/egress_proxy.py` — each worker gets its own `EgressProxy` instance scoped to its blueprint and capability's egress policy. |
| 6.5 | Worker lifecycle (prepare/teardown) | Implemented | `orchestrator/worker_lifecycle.py` — `prepare_worker()` runs the full manifest → validate → blueprint → proxy chain. `teardown_worker()` emits summary events and cleans up. |
| 6.6 | Startup validator (sandbox/egress/audit checks) | Implemented | `orchestrator/startup_validator.py` — 3 check groups (sandbox environment, egress posture, audit integrity). Fail-closed: hub refuses to start if critical checks fail. |
| 6.7 | Seccomp profile defined | Implemented | `config/seccomp-default.json` — default-deny with explicit syscall allowlist (read, write, open, close, mmap, etc.). Covers x86_64 and aarch64. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 6.8 | Worker containers actually created and destroyed | **Implemented** | `orchestrator/worker_executor.py` creates one-shot Docker containers from `SandboxBlueprint`. Container is created, started, waited on, result collected, then force-removed. Tested in `test_worker_executor.py`. |
| 6.9 | Worker agent executes tasks inside container | **Implemented** | `worker/worker_agent.py` runs inside the container, reads `/inbox/task.json`, calls Ollama, writes `/outbox/result.json`. Supports `text_generation` task type. 29 tests in `test_worker_executor.py`. |
| 6.10 | Worker lifecycle wired into job dispatch | **Implemented** | `job_manager.py` routes `route.local` tasks through `execute_in_worker()` when `WorkerExecutor` is available. Falls back to direct Ollama call otherwise. `worker.execution_started` and `worker.execution_completed` audit events emitted. |
| 6.11 | Container resource limits enforced | **Implemented** | `mem_limit`, `pids_limit`, and `tmpfs` are now wired from blueprint → executor request body → worker-proxy → Docker (commit `72fb249`, Phase 2B). Worker-proxy enforces caps from `config/worker-proxy-registry.json` before the Docker socket is touched (commit `578adde`). Seccomp runtime enforcement is now verified separately — see row 4.9 and KI-1. |
| 6.12 | Network isolation enforced per-worker | **Implemented** | Blueprint-driven network isolation: workers with empty `egress_allow` get `network_mode="none"` (no network); workers with egress endpoints connect to `drnt-egress-proxy`. Default is no-network (fail closed). `EgressProxy` enforces allowlists at code level. Worker-proxy validator rejects `network_mode` values other than `"none"` or unset, and rejects networks not in `allowed_networks` (Phase 2B). |
| 6.13 | Worker-proxy field-level default-deny on HTTP boundary | **Implemented** | `worker-proxy/models.py` — Pydantic `ConfigDict(extra="forbid")` plus field validators reject `cap_add`, `cpu_period`, `cpu_quota`, `storage_opt`, `mounts`, `command`, `working_dir`, `network_mode != "none"`, `read_only != True`, missing `no-new-privileges`, missing `cap_drop: ["ALL"]`, and missing `drnt.role: worker` label. Permissive payloads fail with HTTP 422 before the Docker socket. (Phase 2B, commit `578adde`.) |
| 6.14 | Worker-proxy image and resource registry | **Implemented** | `config/worker-proxy-registry.json` declares `approved_images`, `allowed_networks`, `allowed_volume_names`, and `caps` (`mem_limit_max`, `pids_limit_max`, `wall_timeout_max`). Default-deny: worker-proxy refuses to start if the registry file is missing or malformed. `worker-proxy/registry.py` — `load_registry()`, `set_active()`, `get_active()`. (Phase 2B, commit `578adde`.) |

---

## Spec 7 — Signal Chain Resilience

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 7.1 | Comprehensive event type taxonomy | Implemented | `orchestrator/events.py` — builders for job lifecycle, WAL, egress, context, override, system, connectivity, hub state, and worker events. 28 test files exercise these. |
| 7.2 | Idempotency key deduplication | Implemented | `orchestrator/idempotency_store.py` — `check_and_store()` returns existing job on duplicate key. `job_manager.py:submit_job()` checks before creating. |
| 7.3 | Connectivity monitor with circuit breakers | Implemented | `orchestrator/connectivity_monitor.py` — per-route `RouteHealth`, three circuit states (CLOSED/OPEN/HALF_OPEN), hysteresis thresholds (2 failures → open, 3 successes → closed). Background probe loop. `JobManager._run_pipeline()` checks `is_route_available()` before cloud dispatch; OPEN routes fail-fast with `route_unavailable`. |
| 7.4 | Stale job recovery on startup | Implemented | `orchestrator/stale_recovery.py` — scans non-terminal jobs, applies recovery actions by state (re-classify, re-dispatch, deliver). Re-dispatch cap of 2 per job. |
| 7.5 | WAL temporal decay evaluator | Implemented | `orchestrator/decay_evaluator.py` — per-level activity windows, per-capability overrides (must be stricter than system defaults), multi-pass startup evaluation for extended downtime. |
| 7.6 | Hub state manager (active/suspended/awaiting_authority) | Implemented | `orchestrator/hub_state.py` — `suspend()`, `resume()`, `confirm_authority()`. Processing gate: `is_processing_allowed()` returns True only when ACTIVE. |
| 7.7 | Hub startup self-check | Implemented | `hub_state.py:startup_self_check()` — transitions to AWAITING_AUTHORITY if no recent heartbeat within timeout. |

### Execution Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 7.8 | Idempotency store survives restart | **Implemented** | `idempotency_store.py` uses write-through SQLite cache via the `state` table. Records loaded on startup. Tested in `test_idempotency_persistence.py` (restart survival, TTL cleanup, fallback). |
| 7.9 | Stale recovery actually recovers jobs | **Implemented** | Recovery infrastructure is complete and tested (`test_spec7d_stale_recovery.py`). Job state now persists via SQLite — non-terminal jobs are loaded on startup and stale recovery finds them. Tested in `test_job_persistence.py:test_stale_recovery_finds_persisted_jobs`. |
| 7.10 | Connectivity probes hit real endpoints | **Partial** | `connectivity_monitor.py:probe_route()` uses `httpx.AsyncClient` to issue HTTP HEAD requests. Probes are wired into the startup lifecycle and background loop. Actual probing depends on running services and network access. |
| 7.11 | Hub failover between multiple hubs | **Partial** | `HubStateManager` supports the state machine and authority confirmation protocol. However, there is no multi-hub discovery, no shared state backend, and no automatic failover. The system assumes exactly one hub with human-initiated switching. |

---

## Phase 4A — Mobile Agent Command Harness (Backend Contract)

Phase 4A.2 establishes the gateway-side backend contract for the governed Agent Proposal / Agent Inbox approval loop: a mobile-origin job is held for human review, surfaces as `proposal_ready`, is enumerable through a paginated inbox endpoint, and is resolved through an idempotent review decision with first-writer guards against override. No native iOS/Watch code, push delivery, or Spec 9 orchestration is included; the contract is HTTP-, schema-, and audit-shaped only. See [`docs/plans/phase-4a-backend-contract.md`](docs/plans/phase-4a-backend-contract.md) for the authoritative plan and amendments through Phase 4A.2.e.

### Schema and models (Phase 4A.2.a / Phase 4A.1 carry-forward)

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 4A.1 | `JobStatus.proposal_ready` non-terminal status | **Implemented** | `orchestrator/models.py:JobStatus.proposal_ready`. Added in commit `7bd5df5`. Tested in `tests/test_phase4a_backend_contract.py::test_jobstatus_proposal_ready_exists_and_serializes`. |
| 4A.2 | `JobStatus.closed_no_action` terminal-neutral status | **Implemented** | `orchestrator/models.py:JobStatus.closed_no_action`. Added in commit `7bd5df5`. Tested in `tests/test_phase4a_backend_contract.py::test_jobstatus_closed_no_action_exists_and_serializes`. |
| 4A.3 | `ClientSource` enum (`phone_app`, `watch_app`) | **Implemented** | `orchestrator/models.py:ClientSource`. Added in commit `7bd5df5`. Exercised through `tests/test_phase4a_backend_contract.py::test_jobsubmitrequest_accepts_watch_app_client_source` and `::test_jobsubmitrequest_rejects_invalid_client_source`. |
| 4A.4 | `Proposal` model with locked field set | **Implemented** | `orchestrator/models.py:Proposal` with exactly `proposal_id`, `job_id`, `result_id`, `response_hash`, `proposed_by`, `governing_capability_id`, `confidence`, `auto_accept_at` — no `available_actions`, no `confidence_band`. Added in commit `7bd5df5`. Tested in `tests/test_phase4a_backend_contract.py::test_proposal_accepts_valid_payload`, `::test_proposal_auto_accept_at_defaults_to_none`, `::test_proposal_rejects_missing_required_field`. |
| 4A.5 | `ReviewDecision(str, Enum)` locked vocabulary | **Implemented** | `orchestrator/models.py:ReviewDecision` with exactly `approve`, `edit`, `reject`, `defer`, `decline_to_act`. Plan amendment commit `dde0de6` retyped `ReviewRequest.decision`; schema delta in commit `8f1a2ef`. Tested in `tests/test_phase4a_backend_contract.py::test_reviewdecision_has_exactly_five_settled_values`, `::test_reviewrequest_accepts_each_valid_decision`, `::test_reviewrequest_rejects_invalid_decision_value`. |
| 4A.6 | `ReviewRequest.modified_result` schema slot | **Implemented** | `orchestrator/models.py:ReviewRequest.modified_result: Optional[str] = None` with `extra="forbid"`. The required-iff-`edit` constraint is enforced handler-side (see 4A.15). Plan amendment commit `dde0de6`; schema delta in commit `8f1a2ef`. Tested in `tests/test_phase4a_backend_contract.py::test_reviewrequest_accepts_modified_result_string`, `::test_reviewrequest_preserves_modified_result`, `::test_reviewrequest_accepts_omitted_modified_result`, `::test_reviewrequest_accepts_explicit_none_modified_result`, `::test_reviewrequest_rejects_unknown_field`. |
| 4A.7 | `JobStatusResponse.proposal` optional field | **Implemented** | `orchestrator/models.py:JobStatusResponse.proposal: Optional[Proposal] = None`. Added in commit `7bd5df5`. Tested in `tests/test_phase4a_backend_contract.py::test_jobstatusresponse_proposal_defaults_to_none`, `::test_jobstatusresponse_accepts_proposal_object`. |
| 4A.8 | `JobListResponse` wrapper shape | **Implemented** | `orchestrator/models.py:JobListResponse` with `items: list[JobStatusResponse]`, `next_cursor: Optional[str] = None`, `count: int`. Added with the listing endpoint in commit `6ed818f`. Tested in `tests/test_phase4a_jobs_listing.py::test_response_wrapper_shape_includes_items_next_cursor_count`, `::test_count_equals_len_items`. |

### Proposal population (Phase 4A.2.b)

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 4A.9 | `proposal_ready` is reached only via the result-holding review-gate path | **Implemented** | `orchestrator/job_manager.py:_run_pipeline()` captures `delivery_hold` and `hold_type` from the permission result and calls `_derive_proposal_hold_reason()` at the response/result boundary. `pre_action` and `cost_approval` holds are explicitly excluded (no result artifact yet). Plan amendments `8377884`, `c238594`. Implementation in commit `eff74ee`. Tested in `tests/test_phase4a_proposal_population.py::test_delivery_hold_transitions_to_proposal_ready`, `::test_delivery_hold_yields_pre_delivery_hold_reason`, `::test_on_accept_hold_transitions_to_proposal_ready_with_hold_reason`, `::test_pre_action_and_cost_approval_holds_do_not_become_proposal_ready`. |
| 4A.10 | Durable `job.proposal_ready` event records the held-result decision | **Implemented** | `orchestrator/events.py:event_job_proposal_ready()` and `_run_pipeline` await its durable emit after the result/response artifact is recorded and before the pipeline halts. Payload includes `proposal_id`, `result_id`, `response_hash`, `proposed_by`, `governing_capability_id`, `confidence`, `auto_accept_at`, and `hold_reason ∈ {pre_delivery, on_accept}`. Tested in `tests/test_phase4a_proposal_population.py::test_proposal_ready_emits_durable_job_proposal_ready_event`, `::test_proposal_ready_payload_includes_required_fields`. |
| 4A.11 | `JobStatusResponse.proposal` populated centrally via `JobManager.get_proposal` | **Implemented** | `orchestrator/job_manager.py:JobManager.get_proposal()` is the single derivation site. `orchestrator/main.py:get_job` calls the helper and threads the returned `Proposal` into `JobStatusResponse`. The HTTP route does not duplicate proposal-derivation rules. Tested in `tests/test_phase4a_proposal_population.py::test_get_proposal_returns_proposal_for_proposal_ready_job`, `::test_get_proposal_returns_none_for_unknown_job`, `::test_get_proposal_returns_none_for_non_proposal_ready_status`, `::test_non_held_job_still_reaches_delivered_and_get_proposal_returns_none`. |
| 4A.12 | `proposed_by` is the producing model identifier; `auto_accept_at` is null in v1 | **Implemented** | `JobManager.get_proposal()` sets `proposed_by=job.candidate_models[0]` (the literal producing model tag captured at proposal time) and `auto_accept_at=None`. Phase 4A does not introduce automatic acceptance from `proposal_ready`. Tested in `tests/test_phase4a_proposal_population.py::test_proposed_by_is_producing_model_identifier`, `::test_proposal_auto_accept_at_is_none`, `::test_new_job_fields_survive_persistence_roundtrip`. |

### Review endpoint (Phase 4A.2.c / Phase 4A.2.d)

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 4A.13 | `POST /jobs/{job_id}/review` registered and dispatched to `JobManager.review_job` | **Implemented** | `orchestrator/main.py:review_job` route delegates to `orchestrator/job_manager.py:JobManager.review_job` and returns the handler-built `(status_code, body)` envelope verbatim. Plan amendments `6456033`, `5068856`. Implementation in commit `8b3946e`. Tested in `tests/test_phase4a_review_endpoint.py::test_route_returns_200_on_approve`, `::test_route_returns_404_for_unknown_job`, `::test_route_returns_422_on_modified_result_violation`, `::test_route_returns_409_on_stale_decision`. |
| 4A.14 | Per-decision behavior locked: `approve`, `edit`, `reject`, `defer`, `decline_to_act` | **Implemented** | `JobManager._review_approve / _review_edit / _review_reject / _review_defer / _review_decline_to_act` per Phase 4A.2.c rule 5. Approve and edit deliver; edit replaces the authoritative `result_id`/`response_hash` with the modified artifact; reject fails the job; defer is non-terminal and leaves the job reviewable with a fresh idempotency key permitted; decline_to_act transitions to `closed_no_action`. Tested in `tests/test_phase4a_review_endpoint.py::test_approve_transitions_to_delivered`, `::test_edit_transitions_to_delivered_with_updated_authoritative_result`, `::test_reject_transitions_to_failed`, `::test_defer_leaves_job_in_proposal_ready_and_permits_later_review`, `::test_decline_to_act_transitions_to_closed_no_action_and_emits_event`, `::test_terminal_decisions_emit_expected_lifecycle_event`, `::test_defer_does_not_emit_terminal_lifecycle_event`. |
| 4A.15 | Handler-side `modified_result` validation returns HTTP 422 | **Implemented** | `JobManager.review_job` enforces `modified_result` required iff `decision == "edit"`; violations return `(422, …)` before any state mutation or durable emit. Tested in `tests/test_phase4a_review_endpoint.py::test_edit_requires_modified_result_returns_422`, `::test_non_edit_with_modified_result_returns_422`, `::test_route_returns_422_on_modified_result_violation`. |
| 4A.16 | Wrong-status and stale-decision conflicts return the locked 409 body | **Implemented** | `JobManager._review_wrong_status_response()` and `_review_stale_response()` return the locked envelope with `error`, `current_status`, `current_result_id`, `current_response_hash`, `message`. Tested in `tests/test_phase4a_review_endpoint.py::test_review_on_non_proposal_ready_returns_409_locked_body`, `::test_stale_result_id_returns_409_locked_body`, `::test_stale_response_hash_returns_409_locked_body`, `::test_review_when_override_type_already_set_returns_409`, `::test_fresh_key_after_terminal_decision_returns_wrong_status_409`. |
| 4A.17 | `review_idem:` idempotency namespace with replay-before-stale ordering | **Implemented** | `orchestrator/idempotency_store.py:IdempotencyStore.store_review_outcome / get_review_outcome` use the `review_idem:` prefix in the existing `state` table — no second store, no SQL migration. `JobManager.review_job` evaluates idempotency replay before stale-decision checks; same-key/same-payload replays the original `(status_code, body)` envelope without repeating state transitions or durable emits, and same-key/different-payload returns HTTP 409. Records persist across restart via the existing SQLite-backed persistence path. Tested in `tests/test_phase4a_review_endpoint.py::test_same_key_same_payload_replays_original_outcome`, `::test_replay_does_not_emit_duplicate_human_reviewed`, `::test_same_key_different_payload_returns_409`, `::test_review_idem_record_survives_idempotency_store_restart`. |
| 4A.18 | `human.reviewed` carries `ReviewDecision` vocabulary verbatim from the review endpoint | **Implemented** | `JobManager._review_*` branches emit `event_human_reviewed(decision=ReviewDecision.X.value)` so the audit record preserves `approve | edit | reject | defer | decline_to_act` with no translation. The pre-existing `human.reviewed` strings emitted by `override_job` (`modified`) and the auto-accept loop (`auto_delivered`) are unchanged and are not new vocabulary added by Phase 4A. Tested in `tests/test_phase4a_review_endpoint.py::test_human_reviewed_uses_review_decision_vocabulary_exactly`, `::test_edit_human_reviewed_includes_modified_result_lineage`. |
| 4A.19 | Durable `job.closed_no_action` event distinct from `human.reviewed` | **Implemented** | `orchestrator/events.py:event_job_closed_no_action()` and `JobManager._review_decline_to_act` emit it after the winning `decline_to_act` decision is recorded and before the handler returns. Payload includes `result_id`, `response_hash`, `review_decision="decline_to_act"`, `decision_idempotency_key`, `governing_capability_id`, `reason="decline_to_act"`. Tested in `tests/test_phase4a_review_endpoint.py::test_decline_to_act_transitions_to_closed_no_action_and_emits_event`, `::test_closed_no_action_payload_includes_required_fields`. |
| 4A.20 | Review/override first-writer serialization on `proposal_ready` jobs | **Implemented** | `Job.review_decision` (added in `orchestrator/models.py`) is the review-side first-writer guard, distinct from `override_type`. `JobManager.review_job` rejects review when `job.override_type` is set; `JobManager.override_job` returns no-op when `job.review_decision` is in the terminal review-decision set, and on `proposal_ready` jobs sets `override_type` before any durable `human.override` or terminal lifecycle event emit. Terminal review branches set `review_decision` before any durable `human.reviewed` / `job.delivered` / `job.failed` / `job.closed_no_action` emit. Tested in `tests/test_phase4a_review_endpoint.py::test_review_decision_set_before_durable_emit`, `::test_override_after_review_returns_no_op_and_does_not_mutate`, `::test_override_on_proposal_ready_sets_override_type_before_emit`, `::test_review_when_override_type_already_set_returns_409`. |

### Jobs listing endpoint (Phase 4A.2.e)

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 4A.21 | `GET /jobs` registered with required `status` filter and bounded `limit` | **Implemented** | `orchestrator/main.py:list_jobs` accepts `status: JobStatus` (required), `since: Optional[str]`, `limit: int = Query(default=50, ge=1, le=200)`. FastAPI returns HTTP 422 for missing/invalid `status` and out-of-range `limit`. Plan amendment commit `cb36f18`; implementation in commit `6ed818f`. Tested in `tests/test_phase4a_jobs_listing.py::test_missing_status_returns_422`, `::test_invalid_status_returns_422`, `::test_limit_below_min_returns_422`, `::test_limit_above_max_returns_422`, `::test_limit_omitted_defaults_to_50`, `::test_status_proposal_ready_returns_only_proposal_ready_jobs`. |
| 4A.22 | `since` is an exclusive UUIDv7 cursor; malformed values return 422 | **Implemented** | `orchestrator/main.py:list_jobs` validates `since` via `uuid_utils.UUID(since)` and raises HTTP 422 on parse failure. The cursor need not be present in the filtered set; `JobManager.list_jobs` treats it as a UUID-string boundary so cache turnover does not break paging. Tested in `tests/test_phase4a_jobs_listing.py::test_malformed_since_returns_422`, `::test_since_cursor_not_in_set_is_treated_as_exclusive_boundary`. |
| 4A.23 | Newest-first ordering by descending UUIDv7 `job_id` with no-duplication paging | **Implemented** | `JobManager.list_jobs` sorts matching jobs by `job_id` descending and applies the exclusive `since` boundary; `next_cursor` is the last returned `job_id` when more matching jobs remain after the page, otherwise `null`. Following `next_cursor` faithfully does not duplicate jobs across pages. Tested in `tests/test_phase4a_jobs_listing.py::test_results_are_newest_first_by_job_id_descending`, `::test_next_cursor_is_last_id_when_more_remain`, `::test_next_cursor_is_null_when_no_more_remain`, `::test_since_next_cursor_returns_next_page_older_than_cursor`, `::test_no_duplicates_across_pages_with_next_cursor_paging`, `::test_limit_truncates_page`. |
| 4A.24 | Wrapper response with full `JobStatusResponse` items and proposals populated | **Implemented** | `JobManager.list_jobs` returns `JobListResponse(items=[JobStatusResponse...], next_cursor, count)` and calls `get_proposal()` per item so `proposal_ready` items carry their `Proposal` without route-side duplication. `count == len(items)`. Tested in `tests/test_phase4a_jobs_listing.py::test_response_wrapper_shape_includes_items_next_cursor_count`, `::test_items_are_full_jobstatusresponse_objects`, `::test_proposal_populated_on_proposal_ready_items`, `::test_count_equals_len_items`. |
| 4A.25 | Listing source for Phase 4A.2.e is the `JobManager._jobs` in-memory cache | **Implemented** | `JobManager.list_jobs` iterates `self._jobs.values()` only; `orchestrator/persistence.py` is not modified for this slice. SQLite-backed terminal-job history listing is out of scope for Phase 4A.2.e per the 2026-04-26 listing-contract amendment. `GET /jobs/{job_id}` behavior is unchanged and does not collide with `GET /jobs`. Tested in `tests/test_phase4a_jobs_listing.py::test_get_job_by_id_still_works_and_does_not_collide_with_get_jobs`. |

---

## Cross-Cutting Concerns

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| C.1 | Docker Compose multi-service deployment | Implemented | `docker-compose.yml` — 5 services (audit-log-writer, orchestrator, egress-gateway, ollama, worker-proxy), 8 named volumes, 2 networks. |
| C.2 | Healthcheck endpoints | **Implemented** | `GET /health` exists in `main.py` and checks orchestrator, audit log, and Ollama status. |
| C.3 | Tagged release on GitHub | Implemented | v0.1.0 tag and GitHub release published. Visible at `https://github.com/ljefford2-cmyk/local-first-ai-gateway/releases/tag/v0.1.0`. |
| C.4 | Docker socket attack surface mitigated | **Implemented** | The orchestrator no longer mounts the Docker socket. All container operations are delegated to the `worker-proxy` sidecar via HTTP API. The Docker socket is isolated to the `worker-proxy` service, which is the sole holder of the socket. The orchestrator has zero Docker SDK usage. v0.2 Phase 2B narrowed the worker-proxy HTTP surface itself: every field on `ContainerRunRequest` has an explicit validator, dangerous fields are rejected by `extra="forbid"`, and the image/network/volume/resource allowlist is read from `config/worker-proxy-registry.json` at startup (rows 6.13, 6.14). |
| C.5 | `.gitignore` covers sensitive files | **Partial** | `.gitignore` exists. `.env` is gitignored via `secrets/` convention. Review flagged potential gaps — verify coverage of `__pycache__`, `.pyc`, state files, and editor artifacts. |

---

## Test Coverage Summary

| Category | Files | Scope |
|----------|-------|-------|
| Spec 5 (Override Semantics) | `test_phase5a–5e.py` | Unit (mocked audit client) |
| Spec 6 (Worker Silo) | `test_phase6a–6e.py` | Unit (no real containers) |
| Worker Execution | `test_worker_executor.py` | Unit (mocked Docker SDK) |
| Spec 7 (Signal Chain) | `test_spec7a–7f.py` | Unit (mocked deps) |
| Core modules | `test_capability_registry.py`, `test_context_packager.py`, `test_demotion_engine.py`, `test_permission_checker.py`, `test_promotion_monitor.py`, `test_admin_routes.py`, `test_startup.py`, `test_banked_items.py`, `test_be_ordering.py` | Unit |
| Integration | `test_integration_e2e.py` | Integration (mocked external services) |
| Worker Proxy | `test_worker_proxy.py` | Unit (mocked Docker SDK) |
| Persistence | `test_job_persistence.py`, `test_idempotency_persistence.py`, `test_hub_state_persistence.py`, `test_circuit_breaker_persistence.py` | Unit (SQLite) |
| Dispatch Gating | `test_connectivity_dispatch_gating.py` | Unit |
| Phase 4A (Mobile Agent Command Harness — Backend Contract) | `test_phase4a_backend_contract.py`, `test_phase4a_proposal_population.py`, `test_phase4a_review_endpoint.py`, `test_phase4a_jobs_listing.py` | Unit (TestClient + mocked dependencies) |
| **Total** | **42 test files** | |

**Test taxonomy note:** Tests are collected across 42 test files (32 under `tests/`, 9 under `orchestrator/`, 1 under `audit-log-writer/tests/`). Pre-Phase-4A run snapshot: 772 collected, 740 passed, 27 skipped, 5 failed — every failure is in `tests/test_e2e_v02.py` and requires the Docker Compose stack to be running. The passing tests run against in-process Python objects with mocked I/O. `test_integration_e2e.py` tests the FastAPI app with `TestClient` against mocked backends — integration-level but not end-to-end in the operational sense. Phase 4A backend contract is exercised by 83 focused tests across the four new test files: `test_phase4a_backend_contract.py` (23), `test_phase4a_proposal_population.py` (13), `test_phase4a_review_endpoint.py` (28), `test_phase4a_jobs_listing.py` (19); these tests were added by commits `7bd5df5`, `eff74ee`, `8b3946e`, and `6ed818f` and run against in-process Python objects with mocked I/O (no real audit socket, no real Docker, no real cloud egress).

---

## Known Issues

### KI-1 — Seccomp profile not applied to worker containers [RESOLVED 2026-04-18] (Spec 6 row 4.9, 6.11)

**Resolution (2026-04-18):** Runtime seccomp enforcement is now verified by the fixed-contract e2e probe added in commit `13a7abb`, with failed-path observability improved by commit `1073535`. The probe asserts that `personality(0xFFFFFFFF)` is blocked with `errno=EPERM` inside a worker container while control syscall `getpid()` succeeds. This resolves the specific runtime-enforcement contradiction previously tracked here. The historical analysis below is retained as audit trail.

**Symptom:** The seccomp default-deny profile in `config/seccomp-default.json` is not applied to worker containers at runtime.

**Root cause:** `orchestrator/startup_validator.py` reads the seccomp file's full content into `seccomp_profile_content` (a JSON string of ~14 KB). `orchestrator/main.py:269` passes that content into `WorkerLifecycle`, which threads it through `worker_lifecycle.py:338` into `security_config["seccomp_profile"]`. `worker_executor.py:186-189` then formats `f"seccomp={seccomp_profile_path}"` with the content where Docker expects a file *path*. Docker's `security_opt` `seccomp=` argument requires a path string under PATH_MAX; passing the profile body silently fails to apply the filter.

**Scope of impact:** Layer 1 isolation for worker containers is materially weaker than `STATUS.md` previously claimed. Other Layer 1 controls (`cap_drop: ["ALL"]`, `read_only`, `no-new-privileges`, `network_mode: "none"`, image registry, mem/pids caps) are unaffected and continue to constrain the syscall blast radius indirectly.

**Fix path (out of scope for v0.2):** The orchestrator must pass the seccomp file's *path* to the worker-proxy, and the worker-proxy must mount `config/seccomp-default.json` at a known container-internal path so Docker can resolve it. Worker-proxy already mounts `./config:/var/drnt/config:ro` (commit `578adde`), so the host-side mount exists; the orchestrator-side argument and host-path translation across the orchestrator → worker-proxy → Docker boundary are the remaining work.

**Tracked:** Resolved on 2026-04-18 by commits `1073535` and `13a7abb`. Historical entry retained for audit trail.

---

## Summary: What V1 Actually Is

V1 control-plane and execution-plane implementation. All seven specifications are implemented with test coverage across 38 test files (latest run: 772 collected, 740 passed, 27 skipped, 5 e2e-only failures when the Docker Compose stack is not running). The execution plane creates worker containers with file-based I/O through a Docker-socket-isolated `worker-proxy` sidecar that enforces a field-level default-deny allowlist plus an image/network/volume/resource registry on its HTTP boundary (Phase 2B). Job state, idempotency store, circuit breaker state, and hub state are persisted to SQLite with write-through caching. ConnectivityMonitor gates cloud dispatch via circuit breaker. Runtime seccomp enforcement is now verified for the worker path via the fixed-contract probe described in row 4.9 and KI-1.
