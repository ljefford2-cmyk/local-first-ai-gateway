# Scenario Walkthroughs

Concrete dispatch flows through the DRNT gateway, traced from HTTP request to final job state. Each scenario follows the `_run_pipeline` path in `orchestrator/job_manager.py`.

---

## Scenario 1: Local Task Through Worker Container

A simple factual question classified for local dispatch and executed inside a sandboxed Docker container.

### Input

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "raw_input": "What is the capital of France?",
    "input_modality": "text",
    "device": "watch"
  }'
```

### Pipeline Steps

1. **Submit** -- `submit_job()` creates a Job record with status `submitted`, emits `job.submitted` (durable, blocks until ACK), and enqueues the job ID onto the async pipeline queue.

2. **Classify** -- `classify()` sends the prompt to Ollama (`llama3.1:8b`) with the classification system prompt. Ollama returns:
   ```json
   {"category": "quick_lookup", "local_capable": true, "routing": "local",
    "recommended_model": "local", "confidence": 0.95}
   ```
   The classifier maps `"local"` through `MODEL_MAP` to:
   - `capability_id`: `route.local`
   - `candidate_models`: `["llama3.1:8b"]`
   - `route_id`: `ollama-llama3-local`

   Job status becomes `classified`.

3. **Permission check** -- `PermissionChecker.check_permission()` evaluates `route.local` at WAL-0 for action `dispatch_local`. From `capabilities.json`, the policy has `review_gate: "pre_delivery"` and no cost gate. Result: **allowed** with `delivery_hold: true`.

4. **Worker preparation** -- `WorkerLifecycle.prepare_worker()` runs the manifest-to-context chain:
   - Builds `RuntimeManifest` for `route.local` (worker_type=`ollama_local`, memory=256MB, cpu=60s wall)
   - `ManifestValidator.validate()` passes -- emits `manifest.validated`
   - `BlueprintEngine.generate()` produces a `SandboxBlueprint` -- emits `sandbox.blueprint_created`
   - Creates `EgressProxy` scoped to this worker's egress policy
   - Returns `WorkerContext` with status `prepared` -- emits `worker.prepared`

5. **Dispatch** -- Emits `job.dispatched`. Job status becomes `dispatched`. Since routing is `local` and `has_executor` is `true`, the pipeline takes the worker container branch.

6. **Worker execution** -- `execute_in_worker()` builds the task payload and delegates to `WorkerExecutor.execute()`:
   - Creates sandbox directory at `/var/drnt/workers/{job_id}_{worker_id}/`
   - Writes `task.json` to the inbox:
     ```json
     {
       "task_id": "019078a1-...",
       "task_type": "text_generation",
       "payload": {
         "prompt": "What is the capital of France?",
         "model": "llama3.1:8b"
       }
     }
     ```
   - Creates Docker container (`drnt-worker` image) with sandbox constraints: cap-drop ALL, read-only rootfs, no-new-privileges, 256MB memory, /inbox read-only, /outbox read-write
   - Container starts, runs `worker_agent.py`, calls Ollama, writes result
   - Executor reads `result.json` from the outbox:
     ```json
     {
       "task_id": "019078a1-...",
       "status": "success",
       "result": "The capital of France is Paris.",
       "token_count_in": 12,
       "token_count_out": 8,
       "model": "llama3.1:8b"
     }
     ```
   - Container is removed and sandbox directory is cleaned up

7. **Response handling** -- Emits `model.response` and `job.response_received`. Job status becomes `response_received`. Result is written to `/var/drnt/results/{result_id}.txt`.

8. **Deliver** -- Emits `job.delivered`. Job status becomes `delivered`.

9. **Teardown** -- `teardown_worker()` emits `worker.teardown`, clears rate limiter state, and removes the WorkerContext.

### Audit Events

| # | event_type | Key payload fields |
|---|---|---|
| 1 | `job.submitted` | `raw_input`, `input_modality: "text"`, `device: "watch"` |
| 2 | `job.classified` | `request_category: "quick_lookup"`, `confidence: 0.95`, `routing_recommendation: "local"`, `governing_capability_id: "route.local"` |
| 3 | `wal.permission_check` | `capability_id: "route.local"`, `requested_action: "dispatch_local"`, `current_level: 0`, `result: "allowed"`, `delivery_hold: true` |
| 4 | `manifest.validated` | `capability_id: "route.local"`, `worker_type: "ollama_local"`, `valid: true` |
| 5 | `sandbox.blueprint_created` | `capability_id: "route.local"`, `network_mode: "none"`, `memory_limit: "256m"` |
| 6 | `worker.prepared` | `worker_id`, `job_id`, `capability_id: "route.local"`, `blueprint_id`, `manifest_id` |
| 7 | `job.dispatched` | `target_model: "llama3.1:8b"`, `route_id: "ollama-llama3-local"`, `context_package_id: null` |
| 8 | `worker.execution_started` | `worker_id`, `job_id`, `capability_id: "route.local"`, `image: "drnt-worker"`, `task_type: "text_generation"` |
| 9 | `worker.execution_completed` | `worker_id`, `exit_code: 0`, `success: true`, `latency_ms: 2340`, `token_count_in: 12`, `token_count_out: 8` |
| 10 | `model.response` | `model: "llama3.1:8b"`, `route_id: "ollama-llama3-local"`, `latency_ms: 2340`, `cost_estimate_usd: 0.0`, `finish_reason: "stop"` |
| 11 | `job.response_received` | `model: "llama3.1:8b"`, `result_id`, `response_hash` |
| 12 | `job.delivered` | `delivery_target: "api_response"`, `delivery_method: "poll"` |
| 13 | `worker.teardown` | `worker_id`, `job_id`, `total_egress_requests: 0`, `denied_egress_requests: 0`, `duration_seconds: 2.8` |

### Final State

```
job.status    = "delivered"
job.result    = "The capital of France is Paris."
job.wal_level = 0
```

HTTP response (on subsequent `GET /jobs/{id}`):

```json
{
  "job_id": "019078a1-...",
  "status": "delivered",
  "result": "The capital of France is Paris.",
  "request_category": "quick_lookup",
  "routing_recommendation": "local"
}
```

---

## Scenario 2: Cloud-Routed Task With Context Packaging

An analysis request routed to Claude via the egress gateway, with sensitivity scanning that redacts a zip code.

### Input

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "raw_input": "Compare microservices vs monolith architectures for our office in 90210",
    "input_modality": "text",
    "device": "phone"
  }'
```

### Pipeline Steps

1. **Submit** -- Job created with status `submitted`. `job.submitted` emitted (durable).

2. **Classify** -- Ollama returns:
   ```json
   {"category": "analysis", "local_capable": false, "routing": "cloud",
    "recommended_model": "claude", "confidence": 0.9}
   ```
   Mapped through `MODEL_MAP["claude"]`:
   - `capability_id`: `route.cloud.claude`
   - `candidate_models`: `["claude-sonnet-4-20250514"]`
   - `route_id`: `claude-sonnet-default`

3. **Permission check** -- Evaluates `route.cloud.claude` at WAL-0 for action `dispatch_cloud`. Policy: `review_gate: "pre_delivery"`, `cost_gate_usd: 0.25`. `est_cost` is `None` at this point so cost gate is not triggered. Result: **allowed**, `delivery_hold: true`.

4. **Worker preparation** -- Same manifest/validate/blueprint/proxy chain as Scenario 1, but with `capability_id: "route.cloud.claude"` and `worker_type: "cloud_adapter"`. Egress policy includes `api.anthropic.com:443` as an allowed endpoint.

5. **Context packaging** -- `ContextPackager.package()` scans the raw input against `sensitivity.json` patterns:
   - The zip code `90210` matches the `location` class pattern `\b\d{5}(-\d{4})?\b`
   - Action is `generalize` -- the zip is transformed to `902XX` via `_generalize_location()`
   - No credential, name, or financial patterns match
   - Assembled payload: `"Compare microservices vs monolith architectures for our office in 902XX"`
   - Emits `context.packaged` (with `fields_stripped: [field_id]`) and one `context.strip_detail` event

6. **Dispatch** -- Emits `job.dispatched` with `context_package_id` and `assembled_payload_hash`. The orchestrator sends the assembled (redacted) payload to the egress gateway via HTTP POST:
   ```json
   {
     "job_id": "019078b2-...",
     "route_id": "claude-sonnet-default",
     "capability_id": "route.cloud.claude",
     "target_model": "claude-sonnet-4-20250514",
     "prompt": "Compare microservices vs monolith architectures for our office in 902XX",
     "assembled_payload_hash": "a3f8c1...",
     "wal_permission_check_ref": "019078b2-..."
   }
   ```
   The egress gateway forwards to `https://api.anthropic.com/v1/messages` and returns the response.

7. **Response handling** -- Emits `model.response` and `job.response_received`.

8. **Deliver** -- Emits `job.delivered`. Job status becomes `delivered`.

9. **Teardown** -- Worker context cleaned up, `worker.teardown` emitted.

### Audit Events

| # | event_type | Key payload fields |
|---|---|---|
| 1 | `job.submitted` | `raw_input`, `input_modality: "text"`, `device: "phone"` |
| 2 | `job.classified` | `request_category: "analysis"`, `routing_recommendation: "cloud"`, `governing_capability_id: "route.cloud.claude"` |
| 3 | `wal.permission_check` | `capability_id: "route.cloud.claude"`, `requested_action: "dispatch_cloud"`, `result: "allowed"`, `delivery_hold: true` |
| 4 | `manifest.validated` | `capability_id: "route.cloud.claude"`, `worker_type: "cloud_adapter"`, `valid: true` |
| 5 | `sandbox.blueprint_created` | `capability_id: "route.cloud.claude"`, `memory_limit: "256m"` |
| 6 | `worker.prepared` | `worker_id`, `job_id`, `capability_id: "route.cloud.claude"` |
| 7 | `context.packaged` | `context_package_id`, `fields_stripped: ["<field_id>"]`, `assembled_payload_hash: "a3f8c1..."` |
| 8 | `context.strip_detail` | `field_name: "user_input"`, `original_class: "location"`, `action: "generalized"`, `span: {start_char: 64, end_char: 69}` |
| 9 | `job.dispatched` | `target_model: "claude-sonnet-4-20250514"`, `route_id: "claude-sonnet-default"`, `context_package_id` |
| 10 | `model.response` | `model: "claude-sonnet-4-20250514"`, `route_id: "claude-sonnet-default"`, `latency_ms: 4200`, `cost_estimate_usd: 0.018` |
| 11 | `job.response_received` | `model: "claude-sonnet-4-20250514"`, `result_id`, `response_hash` |
| 12 | `job.delivered` | `delivery_target: "api_response"`, `delivery_method: "poll"` |
| 13 | `worker.teardown` | `worker_id`, `total_egress_requests: 0`, `duration_seconds: 4.9` |

### Final State

```
job.status    = "delivered"
job.result    = "Microservices and monolith architectures represent ..."  (truncated)
job.wal_level = 0
```

The original zip code `90210` never left the hub. The cloud provider received `902XX`.

---

## Scenario 3: Override -- Cancel a Delivered Job

A user cancels a previously delivered job via the override endpoint. The job is revoked and the governing capability receives a conditional demotion.

### Precondition

Scenario 1 completed successfully. Job `019078a1-...` is in status `delivered` with `governing_capability_id: "route.local"`.

### Input

```bash
curl -X POST http://localhost:8000/jobs/019078a1-.../override \
  -H "Content-Type: application/json" \
  -d '{
    "override_type": "cancel",
    "target": "routing",
    "detail": "Response was incorrect, Paris is not what I asked about",
    "device": "phone"
  }'
```

### Pipeline Steps

1. **Lookup** -- `override_job()` finds the job. Status is `delivered`, which is not terminal (failed/revoked), so the override proceeds. `override_type` is `None` (no prior override), so the first-override-wins guard passes.

2. **Route selection** -- Since `job.status == "delivered"`, `pre_delivery` is `False`. The cancel takes the post-delivery path.

3. **Override event** -- Emits `human.override` (durable, source=`human`). Captures `source_event_id` for the revocation chain.

4. **Revocation** -- Emits `job.revoked` with `reason: "user_cancel"` and `override_source_event_id` linking back to the override event. Job status becomes `revoked`. `revoked_at` timestamp is set.

5. **Worker cleanup** -- Any active WorkerContext for this job is popped and torn down (in this case, already cleaned up in Scenario 1).

6. **Conditional demotion** -- `cancel` is in `CONDITIONAL_DEMOTION_TYPES`. `is_sentinel_failure(job.last_failing_capability_id)` returns `False` (no prior sentinel failure). `_demote_for_override()` emits `wal.demoted` for `route.local`, demoting from WAL-0 to WAL-(-1) (suspended).

### Audit Events

| # | event_type | Key payload fields |
|---|---|---|
| 1 | `human.override` | `override_type: "cancel"`, `target: "routing"`, `detail: "Response was incorrect..."`, `device: "phone"` |
| 2 | `job.revoked` | `reason: "user_cancel"`, `override_source_event_id: "<event_1_id>"`, `successor_job_id: null` |
| 3 | `wal.demoted` | `capability_id: "route.local"`, `from_level: 0`, `to_level: -1`, `trigger: "override"`, `override_type: "cancel"` |

### Final State

```
job.status        = "revoked"
job.override_type = "cancel"
job.revoked_at    = "2026-04-04T14:30:01.123456Z"
```

HTTP response from the override endpoint:

```json
{
  "status": "revoked",
  "job_id": "019078a1-..."
}
```

After this demotion, `route.local` is at WAL-(-1) (suspended). Any subsequent job classified to `route.local` will be **blocked** at the permission check step with `block_reason: "suspended"` until an operator manually restores the capability.

---

## Scenario 4: Worker Execution Fallback (No Docker)

Same input as Scenario 1, but the `WorkerExecutor` is not available. The pipeline degrades gracefully to a direct `generate_local_response()` call.

### Precondition

`WorkerLifecycle` is initialized but was constructed with `worker_executor=None` (Docker daemon not accessible or image not built). `has_executor` returns `False`.

### Input

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "raw_input": "What is the capital of France?",
    "input_modality": "text",
    "device": "watch"
  }'
```

### Pipeline Steps

Steps 1-4 are identical to Scenario 1 (submit, classify, permission check, worker preparation). The worker context is successfully created -- manifest validation and blueprint generation do not require Docker.

5. **Dispatch** -- Emits `job.dispatched`. The pipeline evaluates the dispatch branch condition:
   ```python
   if (
       worker_ctx is not None                          # True
       and self._worker_lifecycle is not None           # True
       and hasattr(self._worker_lifecycle, "has_executor")  # True
       and self._worker_lifecycle.has_executor           # False  <-- fails here
   ):
   ```
   The condition fails at `has_executor`. The pipeline falls through to **Branch 2**: direct local generation.

6. **Direct generation** -- `generate_local_response()` calls Ollama directly (no container, no sandbox). The prompt is sent to `http://ollama:11434/api/generate` and the response is returned as a `LocalResponseResult`.

7. **Response handling** -- `model.response` and `job.response_received` emitted. Result written to disk.

8. **Deliver** -- `job.delivered` emitted.

9. **Teardown** -- Worker context is still torn down (it was prepared in step 4), emitting `worker.teardown`.

### Audit Events

| # | event_type | Key payload fields |
|---|---|---|
| 1 | `job.submitted` | `raw_input`, `device: "watch"` |
| 2 | `job.classified` | `request_category: "quick_lookup"`, `routing_recommendation: "local"`, `governing_capability_id: "route.local"` |
| 3 | `wal.permission_check` | `capability_id: "route.local"`, `requested_action: "dispatch_local"`, `result: "allowed"` |
| 4 | `manifest.validated` | `capability_id: "route.local"`, `valid: true` |
| 5 | `sandbox.blueprint_created` | `capability_id: "route.local"`, `memory_limit: "256m"` |
| 6 | `worker.prepared` | `worker_id`, `job_id`, `capability_id: "route.local"` |
| 7 | `job.dispatched` | `target_model: "llama3.1:8b"`, `route_id: "ollama-llama3-local"` |
| 8 | `model.response` | `model: "llama3.1:8b"`, `latency_ms: 1800`, `cost_estimate_usd: 0.0` |
| 9 | `job.response_received` | `model: "llama3.1:8b"`, `result_id`, `response_hash` |
| 10 | `job.delivered` | `delivery_target: "api_response"` |
| 11 | `worker.teardown` | `worker_id`, `total_egress_requests: 0`, `duration_seconds: 2.1` |

### What's Different From Scenario 1

Events **8** and **9** from Scenario 1 (`worker.execution_started` and `worker.execution_completed`) are **absent**. The worker was prepared but never executed in a container. The audit trail makes this visible: any job that has `worker.prepared` without a matching `worker.execution_started` used the fallback path.

### Final State

```
job.status    = "delivered"
job.result    = "The capital of France is Paris."
job.wal_level = 0
```

The result is identical to Scenario 1. The difference is operational: the response ran without container isolation. An auditor can detect this by checking for the missing `worker.execution_started` event.

---

## Scenario 5: Cloud Route Health Failure

A cloud route's egress gateway returns an error (circuit breaker open after consecutive failures). The job fails with no automatic fallback to local.

### Input

```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "raw_input": "Write a detailed comparison of React vs Vue for enterprise applications",
    "input_modality": "text",
    "device": "phone"
  }'
```

### Pipeline Steps

1. **Submit** -- Job created, `job.submitted` emitted.

2. **Classify** -- Ollama classifies as `analysis`, routes to cloud/claude:
   - `capability_id`: `route.cloud.claude`
   - `candidate_models`: `["claude-sonnet-4-20250514"]`
   - `route_id`: `claude-sonnet-default`

3. **Permission check** -- `dispatch_cloud` at WAL-0 is **allowed** (same as Scenario 2).

4. **Worker preparation** -- Succeeds normally (manifest, blueprint, WorkerContext created).

5. **Context packaging** -- Input is scanned. No sensitivity matches. Assembled payload is the raw input unchanged. `context.packaged` emitted with `fields_included: [field_id]`, `fields_stripped: []`.

6. **Dispatch** -- `job.dispatched` emitted. The orchestrator POSTs to the egress gateway. The egress gateway's connectivity monitor has marked `claude-sonnet-default` as unhealthy (3 consecutive failures). It returns:
   ```json
   {
     "status": "blocked",
     "failure_type": "circuit_breaker_open",
     "detail": "Route claude-sonnet-default: 3 consecutive failures, circuit open"
   }
   ```

7. **Failure** -- The pipeline reads `status: "blocked"` from the egress response. It emits `job.failed` with `error_class: "circuit_breaker_open"`. Job status becomes `failed`. `last_failing_capability_id` is set to `"circuit_breaker_open"`.

8. **Worker cleanup** -- Worker context is marked as `failed` and torn down. `worker.teardown` emitted.

### Audit Events

| # | event_type | Key payload fields |
|---|---|---|
| 1 | `job.submitted` | `raw_input`, `device: "phone"` |
| 2 | `job.classified` | `request_category: "analysis"`, `routing_recommendation: "cloud"`, `governing_capability_id: "route.cloud.claude"` |
| 3 | `wal.permission_check` | `capability_id: "route.cloud.claude"`, `requested_action: "dispatch_cloud"`, `result: "allowed"` |
| 4 | `manifest.validated` | `capability_id: "route.cloud.claude"`, `valid: true` |
| 5 | `sandbox.blueprint_created` | `capability_id: "route.cloud.claude"` |
| 6 | `worker.prepared` | `worker_id`, `job_id`, `capability_id: "route.cloud.claude"` |
| 7 | `context.packaged` | `fields_included: ["<field_id>"]`, `fields_stripped: []` |
| 8 | `job.dispatched` | `target_model: "claude-sonnet-4-20250514"`, `route_id: "claude-sonnet-default"` |
| 9 | `job.failed` | `error_class: "circuit_breaker_open"`, `detail: "Route claude-sonnet-default: 3 consecutive failures, circuit open"` |
| 10 | `worker.teardown` | `worker_id`, `job_id`, `duration_seconds: 0.3` |

### What's Not Implemented

In v0.1, there is no automatic fallback from a failed cloud route to local dispatch. The `egress.fallback_to_local` event exists but only fires in the opposite direction: when the classifier already routes locally and the code detects that cloud routes are disabled (an informational audit entry, not a re-routing mechanism). A cloud dispatch failure is terminal for the job.

### Final State

```
job.status = "failed"
job.error  = "circuit_breaker_open: Route claude-sonnet-default: 3 consecutive failures, circuit open"
```

HTTP response (on `GET /jobs/{id}`):

```json
{
  "job_id": "019078c3-...",
  "status": "failed",
  "error": "circuit_breaker_open: Route claude-sonnet-default: 3 consecutive failures, circuit open",
  "request_category": "analysis",
  "routing_recommendation": "cloud",
  "result": null
}
```

---

## Reading This Document

- These scenarios trace the code as of **v0.1**. The pipeline, event types, and fallback behaviors reflect the current implementation and may change in future versions.
- For claim-level implementation status, see [STATUS.md](../STATUS.md).
- For worker execution details (sandbox constraints, file I/O schema, security tradeoffs), see [docs/WORKER-EXECUTION.md](WORKER-EXECUTION.md).
- For spec-to-code mapping, see [docs/SPEC-MAP.md](SPEC-MAP.md).
