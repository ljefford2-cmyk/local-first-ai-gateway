# Worker Execution

How the DRNT orchestrator runs tasks inside sandboxed Docker containers.

## Execution Path

```
POST /jobs
  |
  v
classify (Ollama /api/generate)
  |
  v
permission check (WAL level + review gate)
  |
  v
prepare (manifest -> validate -> blueprint -> egress proxy -> WorkerContext)
  |
  v
execute_in_worker (build task.json payload)
  |
  v
POST /containers/run to worker-proxy sidecar (create / start / wait / remove)
  |
  v
read result (parse /outbox/result.json -> WorkerResult)
  |
  v
audit (emit execution_started + execution_completed events)
  |
  v
deliver (write result file, emit job.delivered)
  |
  v
teardown (emit worker.teardown, clear rate limiter, remove context)
```

## File-Based I/O Model

The orchestrator and worker communicate exclusively through JSON files mounted
into the container via Docker volumes.

### task.json (written by orchestrator to /inbox)

```json
{
  "task_id": "job-uuid",
  "task_type": "text_generation",
  "payload": {
    "prompt": "user input text",
    "model": "llama3.1:8b",
    "options": {}
  }
}
```

| Field | Type | Description |
|---|---|---|
| `task_id` | string | Job ID from the orchestrator pipeline |
| `task_type` | string | Handler to invoke (`text_generation` in v1) |
| `payload.prompt` | string | Required. The user prompt |
| `payload.model` | string | Ollama model tag (default `llama3.1:8b`) |
| `payload.options` | object | Optional Ollama generation parameters |

### result.json (written by worker to /outbox)

```json
{
  "task_id": "job-uuid",
  "status": "success",
  "result": "model response text",
  "token_count_in": 123,
  "token_count_out": 456,
  "model": "llama3.1:8b",
  "wall_seconds": 45.3,
  "error": null
}
```

| Field | Type | Description |
|---|---|---|
| `task_id` | string | Echoed from task.json |
| `status` | string | `success` or `error` |
| `result` | string | Model response text (on success) |
| `token_count_in` | int | Prompt token count from Ollama |
| `token_count_out` | int | Completion token count from Ollama |
| `model` | string | Model that generated the response |
| `wall_seconds` | float | Wall-clock execution time |
| `error` | string | Error message (on failure, absent on success) |

## Container Sandbox Constraints

| Constraint | Value | Rationale |
|---|---|---|
| cap-drop | ALL | No Linux capabilities granted |
| Read-only rootfs | true | Prevents container filesystem writes |
| no-new-privileges | true | Blocks suid/sgid escalation |
| Memory limit | 256m | Prevents OOM from affecting host |
| PIDs limit | 64 | Prevents fork bombs |
| User | non-root | Container image runs as unprivileged user |
| /inbox mount | read-only | Worker cannot modify its own task |
| /outbox mount | read-write | Only writable mount for result output |
| tmpfs /tmp | size=64m, noexec, nosuid | Scratch space, no binary execution |
| Network | drnt-internal | Scoped to internal Docker network |

## Audit Events

All events are emitted as durable (blocking until ACK by the audit log writer).

| Event Type | When | Key Payload Fields |
|---|---|---|
| `worker.prepared` | WorkerContext created after manifest validation + blueprint generation | `worker_id`, `job_id`, `capability_id`, `blueprint_id`, `manifest_id` |
| `worker.execution_started` | Container created and started | `worker_id`, `job_id`, `capability_id`, `container_id`, `image`, `task_type` |
| `worker.execution_completed` | Container finished (success or failure) | `worker_id`, `job_id`, `capability_id`, `container_id`, `exit_code`, `success`, `latency_ms`, `token_count_in`, `token_count_out`, `error` |
| `worker.teardown` | Worker context cleaned up | `worker_id`, `job_id`, `total_egress_requests`, `denied_egress_requests`, `duration_seconds` |

## Graceful Fallback Behavior

When the worker executor is unavailable (`has_executor` is false), the
orchestrator does not attempt container execution. The `execute_in_worker`
method raises `WorkerPreparationError` immediately, and the pipeline falls
back to its non-worker dispatch path (direct Ollama call or cloud egress).

If a container runs but produces no `result.json`, the executor reads the
container logs (tail 200 lines) and returns `WorkerResult(success=False)`
with the logs in the error field. The pipeline treats this the same as any
model error and may trigger recovery re-dispatch (up to 2 retries per
Spec 7D).

If Docker itself is unreachable (socket down, daemon stopped), the
`check_docker_available()` startup check fails and the hub startup
validation blocks the orchestrator from accepting jobs (Spec 6D).

## Non-Goals

| Non-Goal | Reason |
|---|---|
| Per-worker network isolation | v1 uses shared `drnt-internal` network; per-container network namespaces are v2 |
| Cloud tasks in workers | Cloud routing goes through the egress gateway, not worker containers |
| Container pooling | Workers are one-shot; pooling warm containers adds complexity without v1 benefit |
| GPU passthrough | v1 targets CPU-only Ollama inference on Raspberry Pi hardware |
| Persistent state | Workers are stateless; every task starts from a clean container |
| Multi-task batching | One task per container; batching adds scheduling complexity |
| Seccomp custom profile | v1 uses Docker's default seccomp profile; custom profiles are v2 |
| Disk quota enforcement | v1 relies on read-only rootfs + tmpfs size limits; `storage_opt` quota requires specific storage drivers |

## Security Tradeoffs

| Risk | Current State | Mitigation | v2 Path |
|---|---|---|---|
| Docker socket (rw) | Docker socket is mounted only on the `worker-proxy` sidecar; orchestrator has zero Docker SDK usage | Sidecar exposes a constrained HTTP API surface; GC loop removes orphaned stopped containers | Rootless Docker or Podman with socket proxy that filters API calls |
| Shared network | All workers share `drnt-internal` bridge network | Workers have no listening ports; egress proxy validates outbound requests at code level | Per-worker network namespace with iptables rules enforced by the proxy |
| Image trust | Worker image is built locally, not pulled from a registry | Image hash is validated at startup (Spec 6D); no `docker pull` at runtime | Image signing with Notary/cosign and admission control |

## Acceptance Criteria

- [x] Worker containers execute tasks using file-based I/O (task.json / result.json)
- [x] All sandbox constraints enforced (cap-drop ALL, read-only rootfs, no-new-privileges, memory/pids limits)
- [x] Full audit trail emitted (prepared, execution_started, execution_completed, teardown)
- [x] Graceful fallback when executor is unavailable or container fails
