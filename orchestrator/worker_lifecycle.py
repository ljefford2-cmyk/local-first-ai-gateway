"""Worker lifecycle manager for Phase 6E: Worker Lifecycle Integration.

Wires Phases 6A–6D into the existing DRNT pipeline. Manages the
full prepare/execute/teardown cycle for workers executing jobs.

The chain: manifest → validate → blueprint → proxy → startup
becomes part of how jobs actually execute.

Priority #5 addition: execute_in_worker() bridges WorkerLifecycle and
WorkerExecutor so the orchestrator can run tasks end-to-end.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from audit_client import AuditLogClient
from blueprint_engine import BlueprintEngine
from capability_registry import CapabilityRegistry
from capability_state import CapabilityStateManager
from egress_proxy import EgressPolicy, EgressProxy
from egress_rate_limiter import EgressRateLimiter
from events import (
    event_manifest_validated,
    event_blueprint_created,
    event_worker_prepared,
    event_worker_preparation_failed,
    event_worker_teardown,
    event_worker_execution_started,
    event_worker_execution_completed,
)
from manifest_validator import ManifestValidator, ManifestValidationResult
from models import Job
from runtime_manifest import (
    NetworkPolicy,
    ResourceLimits,
    RuntimeManifest,
    SecurityPolicy,
    VolumeMount,
    WORKER_TYPE_MIN_WAL,
)
from sandbox_blueprint import SandboxBlueprint
from worker_context import WorkerContext

logger = logging.getLogger(__name__)


class EgressPolicyStore:
    """Stores per-capability egress policies built from Spec 4 egress routes.

    Maps capability_id → EgressPolicy. Used by WorkerLifecycle to
    create scoped EgressProxy instances for each worker.
    """

    def __init__(self) -> None:
        self._policies: dict[str, EgressPolicy] = {}

    def set(self, capability_id: str, policy: EgressPolicy) -> None:
        self._policies[capability_id] = policy

    def get(self, capability_id: str) -> Optional[EgressPolicy]:
        return self._policies.get(capability_id)

    def load_from_routes(self, routes: list[dict]) -> None:
        """Build EgressPolicy entries from Spec 4 egress route data."""
        from urllib.parse import urlparse

        cap_endpoints: dict[str, list[str]] = {}
        cap_rpm: dict[str, int] = {}

        for route in routes:
            url = route.get("endpoint_url", "")
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            endpoint = f"{host}:{port}"
            rpm = route.get("rate_limit_rpm", 30)

            for cap_id in route.get("allowed_capabilities", []):
                if cap_id not in cap_endpoints:
                    cap_endpoints[cap_id] = []
                if endpoint not in cap_endpoints[cap_id]:
                    cap_endpoints[cap_id].append(endpoint)
                # Use the most permissive rate limit across routes
                cap_rpm[cap_id] = max(cap_rpm.get(cap_id, 0), rpm)

        for cap_id, endpoints in cap_endpoints.items():
            self._policies[cap_id] = EgressPolicy(
                capability_id=cap_id,
                allowed_endpoints=endpoints,
                rate_limit_rpm=cap_rpm.get(cap_id, 30),
            )


class WorkerLifecycle:
    """Wires together all Spec 6 components with the existing pipeline.

    Manages the full prepare/teardown cycle for workers executing jobs:
    prepare_worker() creates the worker context, teardown_worker() cleans up.
    """

    def __init__(
        self,
        manifest_validator: ManifestValidator,
        blueprint_engine: BlueprintEngine,
        capability_registry: CapabilityRegistry,
        egress_policy_store: EgressPolicyStore,
        audit_client: AuditLogClient,
        rate_limiter: Optional[EgressRateLimiter] = None,
        state_manager: Optional[CapabilityStateManager] = None,
        worker_executor: Optional[object] = None,
    ):
        self._validator = manifest_validator
        self._blueprint_engine = blueprint_engine
        self._registry = capability_registry
        self._egress_store = egress_policy_store
        self._audit = audit_client
        self._rate_limiter = rate_limiter or EgressRateLimiter()
        self._state_manager = state_manager
        self._worker_executor = worker_executor
        self._active_contexts: dict[str, WorkerContext] = {}
        self._context_start_times: dict[str, float] = {}

    @property
    def active_contexts(self) -> dict[str, WorkerContext]:
        """All currently active worker contexts, keyed by worker_id."""
        return dict(self._active_contexts)

    async def prepare_worker(self, job: Job) -> WorkerContext:
        """Full lifecycle for launching a worker to execute a job.

        Steps:
          1. Look up the governing capability from the job
          2. Create the RuntimeManifest for that capability
          3. Validate the manifest against the capability registry
          4. Generate the SandboxBlueprint from the validated manifest
          5. Create the EgressProxy scoped to this worker
          6. Return a WorkerContext containing all of the above

        If any step fails, the job is failed with error_class
        "worker_preparation_failed".

        Raises:
            WorkerPreparationError: If any step fails.
        """
        capability_id = job.governing_capability_id
        if not capability_id:
            error = "job has no governing_capability_id"
            await self._fail_preparation(job, capability_id or "unknown", error)
            raise WorkerPreparationError(error)

        # Step 1: Look up the governing capability
        cap = self._registry.get(capability_id)
        if cap is None:
            error = f"capability '{capability_id}' not found in registry"
            await self._fail_preparation(job, capability_id, error)
            raise WorkerPreparationError(error)

        # Check capability status via state manager
        if self._state_manager:
            try:
                status = self._state_manager.get_status(capability_id)
                if status != "active":
                    error = f"capability '{capability_id}' is {status}, cannot prepare worker"
                    await self._fail_preparation(job, capability_id, error)
                    raise WorkerPreparationError(error)
            except KeyError:
                error = f"capability '{capability_id}' has no runtime state"
                await self._fail_preparation(job, capability_id, error)
                raise WorkerPreparationError(error)

        # Step 2: Create the RuntimeManifest
        manifest = self._build_manifest_for_capability(cap)

        # Step 3: Validate the manifest
        validation_result = self._validator.validate(manifest)

        # Emit manifest.validated event (pass or fail)
        mv_evt = event_manifest_validated(
            manifest_id=manifest.manifest_id,
            capability_id=capability_id,
            worker_type=manifest.worker_type,
            valid=validation_result.valid,
            error_count=len(validation_result.errors),
            warning_count=len(validation_result.warnings),
            errors=validation_result.errors if not validation_result.valid else None,
        )
        await self._audit.emit_durable(mv_evt)

        if not validation_result.valid:
            error = f"manifest validation failed: {validation_result.errors}"
            await self._fail_preparation(job, capability_id, error)
            raise WorkerPreparationError(error)

        # Step 4: Generate the SandboxBlueprint
        try:
            blueprint = self._blueprint_engine.generate(manifest, validation_result)
        except ValueError as exc:
            error = f"blueprint generation failed: {exc}"
            await self._fail_preparation(job, capability_id, error)
            raise WorkerPreparationError(error)

        # Emit sandbox.blueprint_created event
        bp_evt = event_blueprint_created(
            blueprint_id=blueprint.blueprint_id,
            manifest_id=manifest.manifest_id,
            capability_id=capability_id,
            network_mode=blueprint.network_config.network_mode,
            mount_count=len(blueprint.mounts),
            cap_drop_count=len(blueprint.security_config.cap_drop),
            memory_limit=blueprint.resource_config.memory_limit,
        )
        await self._audit.emit_durable(bp_evt)

        # Step 5: Create the EgressProxy
        policy = self._egress_store.get(capability_id) or EgressPolicy(
            capability_id=capability_id,
            allowed_endpoints=[],
            rate_limit_rpm=30,
        )
        rate_limiter = self._rate_limiter if policy.rate_limit_rpm > 0 else None
        egress_proxy = EgressProxy(
            blueprint=blueprint, egress_policy=policy, rate_limiter=rate_limiter,
            audit_client=self._audit,
        )

        # Step 6: Build and return WorkerContext
        ctx = WorkerContext(
            job_id=job.job_id,
            manifest=manifest,
            validation_result=validation_result,
            blueprint=blueprint,
            egress_proxy=egress_proxy,
            status="prepared",
        )

        self._active_contexts[ctx.worker_id] = ctx
        self._context_start_times[ctx.worker_id] = time.monotonic()

        # Emit worker.prepared event
        prepared_evt = event_worker_prepared(
            worker_id=ctx.worker_id,
            job_id=job.job_id,
            capability_id=capability_id,
            blueprint_id=blueprint.blueprint_id,
            manifest_id=manifest.manifest_id,
        )
        await self._audit.emit_durable(prepared_evt)

        logger.info(
            "Worker %s prepared for job %s (capability=%s, blueprint=%s)",
            ctx.worker_id, job.job_id, capability_id, blueprint.blueprint_id,
        )

        return ctx

    @property
    def has_executor(self) -> bool:
        """Whether a WorkerExecutor is available for execute_in_worker()."""
        return self._worker_executor is not None

    async def execute_in_worker(
        self,
        context: WorkerContext,
        prompt: str,
        model: str = "llama3.1:8b",
        task_type: str = "text_generation",
    ) -> dict:
        """Run a task inside a sandboxed worker container.

        Bridges WorkerLifecycle and WorkerExecutor: builds a task.json
        payload matching the worker_agent schema, calls the executor,
        and returns a result dict matching the result.json schema.

        Raises:
            WorkerPreparationError: If no executor is configured or no
                blueprint exists, or on unexpected errors.
            WorkerExecutionError: If the worker reports failure.
        """
        if self._worker_executor is None:
            raise WorkerPreparationError("no worker_executor configured")
        if context.blueprint is None:
            raise WorkerPreparationError("context has no blueprint")

        capability_id = context.manifest.capability_id

        # Emit worker.execution_started
        started_evt = event_worker_execution_started(
            worker_id=context.worker_id,
            job_id=context.job_id,
            capability_id=capability_id,
            container_id="pending",
            image=context.blueprint.container_config.image,
            task_type=task_type,
        )
        await self._audit.emit_durable(started_evt)

        try:
            task_payload = {
                "task_id": context.job_id,
                "task_type": task_type,
                "payload": {
                    "prompt": prompt,
                    "model": model,
                },
            }

            result = await self._worker_executor.execute(
                worker_id=context.worker_id,
                job_id=context.job_id,
                capability_id=capability_id,
                image=context.blueprint.container_config.image,
                task_payload=task_payload,
                resource_config={
                    "memory_limit": context.blueprint.resource_config.memory_limit,
                    "cpu_period": context.blueprint.resource_config.cpu_period,
                    "cpu_quota": context.blueprint.resource_config.cpu_quota,
                    "pids_limit": context.blueprint.resource_config.pids_limit,
                },
                security_config={
                    "cap_drop": context.blueprint.security_config.cap_drop,
                    "read_only_rootfs": context.blueprint.security_config.read_only_rootfs,
                    "no_new_privileges": context.blueprint.security_config.no_new_privileges,
                    "seccomp_profile": (
                        os.environ.get("DRNT_SECCOMP_PROFILE")
                        if context.blueprint.security_config.seccomp_profile_path
                        and context.blueprint.security_config.seccomp_profile_path != "default"
                        else None
                    ),
                    "network_mode": context.blueprint.network_config.network_mode,
                },
                wall_timeout=context.manifest.resources.max_wall_seconds,
            )

            # Emit worker.execution_completed
            completed_evt = event_worker_execution_completed(
                worker_id=context.worker_id,
                job_id=context.job_id,
                capability_id=capability_id,
                container_id=result.container_id or "unknown",
                exit_code=result.exit_code,
                success=result.success,
                latency_ms=result.latency_ms,
                token_count_in=result.token_count_in,
                token_count_out=result.token_count_out,
                error=result.error,
            )
            await self._audit.emit_durable(completed_evt)

            if not result.success:
                from worker_executor import WorkerExecutionError
                raise WorkerExecutionError(
                    f"worker execution failed: {result.error}"
                )

            return {
                "response_text": result.response_text,
                "token_count_in": result.token_count_in,
                "token_count_out": result.token_count_out,
                "latency_ms": result.latency_ms,
                "model": result.model,
                "container_id": result.container_id,
            }

        except WorkerPreparationError:
            raise
        except Exception as exc:
            # Re-raise WorkerExecutionError as-is
            from worker_executor import WorkerExecutionError
            if isinstance(exc, WorkerExecutionError):
                raise

            # Unexpected error — emit failed completion, then raise
            failed_evt = event_worker_execution_completed(
                worker_id=context.worker_id,
                job_id=context.job_id,
                capability_id=capability_id,
                container_id="unknown",
                exit_code=-1,
                success=False,
                latency_ms=0,
                error=str(exc),
            )
            await self._audit.emit_durable(failed_evt)
            raise WorkerPreparationError(
                f"unexpected error during worker execution: {exc}"
            ) from exc

    async def teardown_worker(self, context: WorkerContext) -> None:
        """Cleanup after job completion.

        Steps:
          1. Emit egress summary event (total requests, denied count)
          2. Clear rate limiter state for this worker
          3. Mark blueprint as inactive
        """
        # Compute duration
        start_time = self._context_start_times.get(context.worker_id)
        if start_time is not None:
            duration = time.monotonic() - start_time
        else:
            duration = 0.0

        # Count egress requests
        total_egress = 0
        denied_egress = 0
        if context.egress_proxy is not None:
            events = context.egress_proxy.events
            total_egress = len(events)
            denied_egress = sum(
                1 for e in events
                if e.get("event_type") == "egress.denied"
            )

        # Emit worker.teardown event
        teardown_evt = event_worker_teardown(
            worker_id=context.worker_id,
            job_id=context.job_id,
            total_egress_requests=total_egress,
            denied_egress_requests=denied_egress,
            duration_seconds=round(duration, 3),
        )
        await self._audit.emit_durable(teardown_evt)

        # Clear rate limiter state for this worker
        if context.blueprint is not None:
            self._rate_limiter.reset(context.blueprint.blueprint_id)

        # Mark context as completed and remove from active set
        context.status = "completed"
        self._active_contexts.pop(context.worker_id, None)
        self._context_start_times.pop(context.worker_id, None)

        logger.info(
            "Worker %s torn down for job %s (egress: %d total, %d denied, %.1fs)",
            context.worker_id, context.job_id,
            total_egress, denied_egress, duration,
        )

    async def _fail_preparation(
        self, job: Job, capability_id: str, error: str
    ) -> None:
        """Emit failure event when worker preparation fails."""
        fail_evt = event_worker_preparation_failed(
            job_id=job.job_id,
            capability_id=capability_id,
            error=error,
        )
        await self._audit.emit_durable(fail_evt)

    def _build_manifest_for_capability(self, cap: dict) -> RuntimeManifest:
        """Build a RuntimeManifest from a capability registry entry.

        v1: Creates a fresh manifest for each job. Manifest caching
        (reusing a validated manifest for the same capability across
        jobs) is v2.
        """
        capability_id = cap["capability_id"]

        # Determine worker_type from capability's provider dependencies
        # and routing hints
        cap_type = cap.get("capability_type", "governing")
        provider_deps = cap.get("provider_dependencies") or []

        if capability_id == "route.local" or not provider_deps:
            worker_type = "ollama_local"
        else:
            worker_type = "cloud_adapter"

        # Build network policy from egress policy store
        policy = self._egress_store.get(capability_id)
        egress_allow = policy.allowed_endpoints if policy else []

        # Standard volume mounts for v1 workers
        volumes = [
            VolumeMount(path="/work", mode="rw", host_path=f"/work/{capability_id}"),
            VolumeMount(path="/tmp", mode="rw", host_path=f"/tmp/{capability_id}"),
        ]

        return RuntimeManifest(
            capability_id=capability_id,
            worker_type=worker_type,
            volumes=volumes,
            network=NetworkPolicy(
                egress_allow=egress_allow,
                egress_deny_all=True,
                ingress="none",
            ),
            resources=ResourceLimits(
                max_memory_mb=256,
                max_cpu_seconds=60,
                max_wall_seconds=120,
                max_disk_mb=100,
            ),
            security=SecurityPolicy(
                drop_capabilities=["ALL"],
                read_only_root=True,
                no_new_privileges=True,
                seccomp_profile="default",
            ),
        )


class WorkerPreparationError(Exception):
    """Raised when worker preparation fails at any step."""
    pass
