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

| Spec | Test Files | Test Functions |
|------|-----------|----------------|
| 1 -- Audit/Event Schema | 1 | 4 |
| 2 -- Capability Model (WAL → Permissions) | 2 | 46 |
| 3 -- Context Boundary Specification | 1 | 22 |
| 4 -- Egress Policy Binding | 3 | 77 |
| 5 -- Override Semantics | 10 | 180 |
| 6 -- Silo Runtime Security | 6 | 176 |
| 7 -- Signal Chain Resilience | 6 | 181 |
| Cross-cutting (admin routes) | 1 | 18 |
| **Total (deduplicated)** | **34** | See [STATUS.md](../STATUS.md) |

Note: `test_phase6c.py` (30 tests) and `test_phase6d.py` (32 tests) are shared between Specs 4 and 6.
Per-spec totals count these tests under both specs; the deduplicated total counts each test once.
Additional test files since initial tally: persistence tests (`test_job_persistence.py`, `test_idempotency_persistence.py`, `test_hub_state_persistence.py`, `test_circuit_breaker_persistence.py`), `test_connectivity_dispatch_gating.py`, `test_worker_proxy.py`, `test_integration_lifecycle.py`.

---

This table is a navigation aid. For claim-level status with evidence, see [STATUS.md](../STATUS.md). For security boundaries, see [THREAT-MODEL.md](../THREAT-MODEL.md). For worker execution details, see [docs/WORKER-EXECUTION.md](WORKER-EXECUTION.md).
