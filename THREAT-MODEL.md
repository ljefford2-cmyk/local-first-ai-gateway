# DRNT v0.1 — Threat Model and Non-Goals

## 1. Scope and Audience

DRNT is a single-user, self-hosted personal AI gateway. The operator is the only user. The deployment environment is a trusted local machine or home server running Docker. This is not a multi-tenant service, not internet-exposed, and not designed for hostile local users. This document defines what V1 assumes, what it defends against, what it does not, and the mitigation path for each known gap.

## 2. Trust Boundaries

**Host ↔ Docker** — The operator's host machine is fully trusted. Docker containers are the isolation boundary. Compromise of the host means full compromise of the system.

**Orchestrator ↔ Workers** — Workers run in sandboxed containers with declared manifests. In V1, the worker execution plane is aspirational — `manifest_validator.py` validates manifests and `blueprint_engine.py` generates sandbox configurations, but no containers are actually created via Docker API. The control-plane enforcement (manifest validation, egress proxy, blueprint engine) exists; the execution-plane enforcement does not. STATUS.md rows 6.8–6.12 are all marked "Aspirational."

**Internal network ↔ External network** — Two Docker networks defined in `docker-compose.yml`: `drnt-internal` (bridge, service mesh) and `drnt-external` (bridge, cloud egress). The orchestrator sits on `drnt-internal` only. The egress gateway bridges both. Ollama is on both (local inference + model pulls).

**Gateway ↔ Cloud providers** — API keys are bind-mounted read-only from the host (`./secrets:/var/drnt/secrets:ro`). All cloud requests go through the egress gateway. The egress proxy in `egress_proxy.py` enforces per-capability allowlists at the code level (not network level in V1).

## 3. V1 Threat Surface — What We Defend Against

**Unauthorized egress** — `egress_proxy.py:authorize()` implements a five-rule evaluation with default-deny. Audit events emitted for every authorized and denied request via `egress_events.py`. Durable logging via `audit_client.py:AuditLogClient`.

**Audit log tampering** — SHA-256 hash chain from genesis (`DRNT-GENESIS` seed) in `audit-log-writer/src/hash_chain.py`. Append-only JSONL on Docker volume `audit-logs`. `fsync` on durable events via `audit-log-writer/src/file_manager.py:append_line()`. Standalone verification tool at `audit-log-writer/scripts/drnt_audit_verify.py`.

**Capability escalation** — WAL (Work Authorization Level) system implemented across `capability_state.py`, `promotion_monitor.py`, and `demotion_engine.py`. Promotion criteria, demotion engine (3 failures in 24h), and human override semantics in `job_manager.py:override_job()`. Every state change is audited.

**Manifest violations** — `manifest_validator.py:validate()` runs 10 checks: capability existence, active status, WAL level, volume paths (forbidden path and audit overlap detection), egress deny-all flag, egress endpoint authorization, capability drops, no-new-privileges, memory ceiling, and wall-time ceiling. Fail-closed.

**Context data leakage** — `context_packager.py` with sensitivity classification via `SensitivityPattern`. Regex-based PII detection in `_scan()` strips or generalizes sensitive fields before cloud routing. Actions: strip (replace with `[REDACTED:class]`), generalize (zip codes → `315XX`, dates → year), or pass.

**Stale job recovery** — `decay_evaluator.py` with configurable per-WAL-level activity windows (`window_days`, `min_outcomes`). `job_manager.py` defines `AUTO_ACCEPT_WINDOW_SECONDS` (default 86400s). Jobs that exceed their window are flagged, not silently processed.

## 4. V1 Non-Goals — What We Explicitly Do NOT Defend Against

**1. Multi-tenant isolation** — V1 is single-user. No user authentication, no role-based access, no tenant separation. Not a bug — it's a personal gateway. Mitigation: none needed for intended use case.

**2. Internet-exposed deployment** — The orchestrator binds to port 8000 with no TLS, no auth, no rate limiting on the API itself. Designed for localhost or trusted LAN only. Mitigation: documented in README. V2 consideration: optional TLS + API key.

**3. Hostile local user** — If another user on the host can access the Docker socket or the filesystem, they have full access. Mitigation: standard Unix file permissions. Not a DRNT concern.

**4. Docker socket escalation** — The orchestrator mounts `/var/run/docker.sock:/var/run/docker.sock:ro`. Read-only prevents container creation but still allows container enumeration, image inspection, and information disclosure. This is the most-discussed attack surface. Mitigation path: V2 moves to a restricted Docker API proxy or socket-activated spawning. V1 accepts this risk because the socket is only used for startup validation — `startup_validator.py:_check_docker_socket()` and `_check_base_image()` query the Docker Engine API to verify image existence.

**5. Secrets at rest** — API keys stored in plaintext `.env` file on a bind-mount (`./secrets:/var/drnt/secrets:ro`). No encryption, no vault, no rotation, no expiry tracking. Mitigation path: V2 integrates with a secrets manager (Docker secrets, HashiCorp Vault). V1 accepts this because the operator controls the host filesystem.

**6. Worker execution-plane enforcement** — The control plane (`manifest_validator.py`, `blueprint_engine.py`, `egress_proxy.py`) is implemented. The execution plane (creating worker containers via Docker API, applying seccomp at runtime, enforcing resource limits on real containers) is aspirational. `BlueprintEngine.generate()` produces a `SandboxBlueprint` with Docker run arguments, but nothing calls `docker.containers.run()`. STATUS.md rows 6.8–6.12 are all "Aspirational." Mitigation: Priority #5 in the gap closure project. This threat model exists partly to scope that work.

**7. Network-level egress enforcement** — The egress proxy is a code-level gate. A compromised worker process could bypass it by making direct HTTP calls. The module docstring in `egress_proxy.py` states: "This is a code-level gate (v1). A true network proxy (iptables/envoy) is v2." Mitigation path: V2 adds iptables rules or Envoy sidecar per worker.

**8. Sensitivity regex brittleness** — `context_packager.py` PII detection uses regex patterns from `config/sensitivity.json`. These are bypassable with encoding, misspelling, or novel PII formats. No Unicode normalization, no homoglyph detection. Mitigation path: V2 considers NER-based detection. V1 accepts regex as a first-pass filter, not a guarantee.

**9. In-memory job state** — Jobs are stored in `job_manager._jobs`, a Python `dict`. No jobs survive orchestrator restarts. The `stale_recovery.py` docstring states: "No jobs survive V1 restarts; this infrastructure exists for when persistence is added." Mitigation path: V2 adds persistent job store (SQLite or similar).

**10. In-memory rate limiting** — `egress_rate_limiter.py:EgressRateLimiter` uses a per-blueprint sliding-window counter stored in `self._windows: dict[str, list[float]]`. Restarts reset all counters. Mitigation path: persistent counter store in V2.

**11. Multi-hub failover** — `hub_state.py:HubStateManager` supports the state machine (ACTIVE/SUSPENDED/AWAITING_AUTHORITY) but there is no multi-hub discovery, no shared state backend, no automatic failover. The system assumes exactly one hub. The module docstring states: "Exactly one hub is authoritative at any time." Mitigation: accepted for V1 single-user scope.

## 5. Docker Compose Security Posture

Current posture in `docker-compose.yml`:

- `restart: unless-stopped` on all four services
- Healthchecks on all services: socket test (audit-log-writer), HTTP `/health` (orchestrator), TCP connect (egress-gateway), HTTP root (ollama)
- Seven named Docker volumes: `audit-socket`, `audit-logs`, `results-store`, `ollama-data`, `drnt-context-store`, `capability-state`, `sandbox-workdir`
- Secrets bind-mounted read-only (`./secrets:/var/drnt/secrets:ro`)
- Config bind-mounted read-only (`./config:/var/drnt/config:ro`)
- Dual network isolation (`drnt-internal` bridge, `drnt-external` bridge)
- `seccomp-default.json` exists in `config/` and is validated at startup by `startup_validator.py`, but is not applied at Docker Compose level via `security_opt`
- No AppArmor or SELinux profiles
- No `read_only: true` on service filesystems (V2 consideration)

## 6. V1 Summary

DRNT v0.1 is a single-user, self-hosted control plane for personal AI orchestration. Its security model assumes a trusted operator on a trusted host. The primary V1 contribution is auditable governance — every decision, override, and egress request is hash-chained into a tamper-evident log. The primary V1 gap is execution-plane enforcement — the architecture describes worker sandboxing in detail, but V1 validates manifests without creating actual containers. This is an intentional sequencing decision: prove the governance model works before adding the execution boundary.
