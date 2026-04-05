# DRNT Gateway — Claim-Status-Evidence Matrix

**Version:** v0.1 (pre-release)
**Generated:** 2026-04-04
**Purpose:** Map every architectural claim to its actual implementation status and evidence. This document exists because the four-model adversarial review (April 2026) converged on a single finding: the governance language is more mature than the runtime, and the public narrative risks outrunning implementation completeness.

**Status definitions:**

- **Implemented** — Code exists, is wired into the runtime, and is exercised by tests.
- **Partial** — Code exists but is incomplete, has known V1 limitations, or is not fully wired.
- **Aspirational** — Described in specs or README but not yet built.

---

## Spec 1 — Audit Log Writer

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

## Spec 2 — Orchestrator Core

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

## Spec 3 — Context Packager

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

## Spec 4 — Egress Policy

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
| 4.9 | Secrets rotation mechanism | **Aspirational** | Secrets are plain `.env` file on a bind-mount. No rotation, no vault integration, no expiry tracking. Identified in adversarial review. |
| 4.10 | Seccomp profile applied to services | **Implemented** | `config/seccomp-default.json` exists with a default-deny policy and explicit syscall allowlist. `worker_lifecycle.py` resolves the profile path via `DRNT_SECCOMP_PROFILE` env var and passes it to `worker_executor.py`, which applies it via `security_opt=["no-new-privileges", "seccomp=<path>"]` on container creation. |

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

## Spec 6 — Worker Silo / Runtime Enforcement

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
| 6.11 | Container resource limits enforced | **Implemented** | Container resource limits (memory, pids) are applied via Docker SDK. Seccomp profile resolved from `DRNT_SECCOMP_PROFILE` env var and passed to container creation via `security_opt`. |
| 6.12 | Network isolation enforced per-worker | **Implemented** | Blueprint-driven network isolation: workers with empty `egress_allow` get `network_mode="none"` (no network); workers with egress endpoints connect to `drnt-egress-proxy`. Default is no-network (fail closed). `EgressProxy` enforces allowlists at code level. |

---

## Spec 7 — Signal Chain Resilience

### Control Plane

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| 7.1 | Comprehensive event type taxonomy | Implemented | `orchestrator/events.py` — builders for job lifecycle, WAL, egress, context, override, system, connectivity, hub state, and worker events. 647 tests across 30 files exercise these. |
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
| 7.9 | Stale recovery actually recovers jobs | **Implemented** | Recovery infrastructure is complete and tested (30 tests in `test_spec7d_stale_recovery.py`). Job state now persists via SQLite — non-terminal jobs are loaded on startup and stale recovery finds them. Tested in `test_job_persistence.py:test_stale_recovery_finds_persisted_jobs`. |
| 7.10 | Connectivity probes hit real endpoints | **Partial** | `connectivity_monitor.py:probe_route()` uses `httpx.AsyncClient` to issue HTTP HEAD requests. Probes are wired into the startup lifecycle and background loop. Actual probing depends on running services and network access. |
| 7.11 | Hub failover between multiple hubs | **Partial** | `HubStateManager` supports the state machine and authority confirmation protocol. However, there is no multi-hub discovery, no shared state backend, and no automatic failover. The system assumes exactly one hub with human-initiated switching. |

---

## Cross-Cutting Concerns

| # | Claim | Status | Evidence |
|---|-------|--------|----------|
| C.1 | Docker Compose multi-service deployment | Implemented | `docker-compose.yml` — 5 services (audit-log-writer, orchestrator, egress-gateway, ollama, worker-proxy), 8 named volumes, 2 networks. |
| C.2 | Healthcheck endpoints | **Implemented** | `GET /health` exists in `main.py` and checks orchestrator, audit log, and Ollama status. |
| C.3 | Tagged release on GitHub | Implemented | v0.1.0 tag and GitHub release published. Visible at `https://github.com/ljefford2-cmyk/local-first-ai-gateway/releases/tag/v0.1.0`. |
| C.4 | Docker socket attack surface mitigated | **Implemented** | The orchestrator no longer mounts the Docker socket. All container operations are delegated to the `worker-proxy` sidecar via HTTP API. The Docker socket is isolated to the `worker-proxy` service, which is the sole holder of the socket. The orchestrator has zero Docker SDK usage. |
| C.5 | `.gitignore` covers sensitive files | **Partial** | `.gitignore` exists. `.env` is gitignored via `secrets/` convention. Review flagged potential gaps — verify coverage of `__pycache__`, `.pyc`, state files, and editor artifacts. |

---

## Test Coverage Summary

| Category | Files | Test Functions | Scope |
|----------|-------|---------------|-------|
| Spec 5 (Override Semantics) | `test_phase5a–5e.py` | 69 | Unit (mocked audit client) |
| Spec 6 (Worker Silo) | `test_phase6a–6e.py` | 147 | Unit (no real containers) |
| Worker Execution | `test_worker_executor.py` | 29 | Unit (mocked Docker SDK) |
| Spec 7 (Signal Chain) | `test_spec7a–7f.py` | 181 | Unit (mocked deps) |
| Core modules | `test_capability_registry.py`, `test_context_packager.py`, `test_demotion_engine.py`, `test_permission_checker.py`, `test_promotion_monitor.py`, `test_admin_routes.py`, `test_startup.py`, `test_banked_items.py`, `test_be_ordering.py` | 174 | Unit |
| Integration | `test_integration_e2e.py` | 27 | Integration (mocked external services) |
| Worker Proxy | `test_worker_proxy.py` | 25 | Unit (mocked Docker SDK) |
| Persistence | `test_job_persistence.py`, `test_idempotency_persistence.py`, `test_hub_state_persistence.py`, `test_circuit_breaker_persistence.py` | 40 | Unit (SQLite) |
| Dispatch Gating | `test_connectivity_dispatch_gating.py` | 18 | Unit |
| **Total** | **34 files** | **735** | |

**Test taxonomy note:** 735 tests collected (717 passed, 18 skipped e2e integration requiring Docker stack). The 717 passing tests run against in-process Python objects with mocked I/O. The `test_integration_e2e.py` file tests the FastAPI app with `TestClient` against mocked backends — it is integration-level but not end-to-end in the operational sense.

---

## Summary: What V1 Actually Is

**V1 is a control-plane implementation with partial execution-plane realization.** The orchestrator, audit log, context packager, egress policy engine, override semantics, worker lifecycle preparation chain, and signal chain resilience modules are implemented and tested. The system correctly produces audit events, enforces WAL permissions, manages capability state, and handles the full job lifecycle through classification and dispatch.

**What V1 is not:** a system with full operational durability. Worker containers are created for `route.local` tasks. Cloud worker execution and per-worker network isolation remain v2 concerns. Job state, idempotency records, circuit breaker state, and hub state now persist across restarts via SQLite write-through caching. Rate limiters intentionally reset on restart. The egress audit trail is wired to the persistent audit log writer via `AuditLogClient`.

**The honest version string:** "V1 control-plane implementation with partial execution-plane realization — audit, orchestration, policy enforcement, state persistence, and worker sandboxing are operational; cloud worker execution and per-worker network isolation remain v2."
