"""WorkerContext dataclass for Phase 6E: Worker Lifecycle Integration.

Holds all Spec 6 artifacts produced during worker preparation —
manifest, validation result, blueprint, and egress proxy — scoped
to a single job execution.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from egress_proxy import EgressProxy
from manifest_validator import ManifestValidationResult
from runtime_manifest import RuntimeManifest
from sandbox_blueprint import SandboxBlueprint


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class WorkerContext:
    """All Spec 6 artifacts for a single job execution.

    Created by WorkerLifecycle.prepare_worker(), consumed by the
    pipeline, and cleaned up by WorkerLifecycle.teardown_worker().
    """
    worker_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str = ""
    manifest: RuntimeManifest = field(default_factory=lambda: RuntimeManifest(
        capability_id="", worker_type="ollama_local"
    ))
    validation_result: ManifestValidationResult = field(
        default_factory=lambda: ManifestValidationResult(valid=False)
    )
    blueprint: SandboxBlueprint = field(default=None)  # type: ignore[assignment]
    egress_proxy: Optional[EgressProxy] = None
    created_at: str = field(default_factory=_now_iso)
    status: str = "prepared"  # "prepared" | "active" | "completed" | "failed"
