"""Image, network, volume, and resource registry for the worker proxy.

Enforces a default-deny allowlist at request validation time. The proxy
refuses to start if the registry file is missing or malformed — there is
no implicit default allow-anything mode.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = "/var/drnt/config/worker-proxy-registry.json"

# Fallback caps used only when the registry's "caps" section omits a field.
_DEFAULT_MEM_LIMIT_MAX = "4g"
_DEFAULT_PIDS_LIMIT_MAX = 512
_DEFAULT_WALL_TIMEOUT_MAX = 3600


@dataclass(frozen=True)
class RegistryCaps:
    mem_limit_max: str = _DEFAULT_MEM_LIMIT_MAX
    pids_limit_max: int = _DEFAULT_PIDS_LIMIT_MAX
    wall_timeout_max: int = _DEFAULT_WALL_TIMEOUT_MAX


@dataclass(frozen=True)
class Registry:
    approved_images: frozenset[str] = field(default_factory=frozenset)
    allowed_networks: frozenset[str] = field(default_factory=frozenset)
    allowed_volume_names: frozenset[str] = field(default_factory=frozenset)
    caps: RegistryCaps = field(default_factory=RegistryCaps)


class RegistryError(Exception):
    """Raised when the registry file is missing or malformed."""


def load_registry(path: Optional[str] = None) -> Registry:
    """Load and validate the registry file.

    Default-deny: any I/O or parse failure raises RegistryError. The proxy
    startup hook catches this and aborts.
    """
    path = path or os.environ.get("DRNT_WORKER_PROXY_REGISTRY", DEFAULT_REGISTRY_PATH)
    if not os.path.exists(path):
        raise RegistryError(
            f"Worker proxy registry not found at {path}. "
            f"Set DRNT_WORKER_PROXY_REGISTRY or create the file."
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"Failed to load registry from {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(
            f"Registry must be a JSON object, got {type(raw).__name__}"
        )

    def _require_list(key: str) -> list[str]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise RegistryError(f"{key} must be a list of strings")
        return value

    approved_images = _require_list("approved_images")
    allowed_networks = _require_list("allowed_networks")
    allowed_volume_names = _require_list("allowed_volume_names")

    if not approved_images:
        raise RegistryError("approved_images must be non-empty (default-deny)")

    caps_raw = raw.get("caps", {})
    if not isinstance(caps_raw, dict):
        raise RegistryError("caps must be an object")

    try:
        caps = RegistryCaps(
            mem_limit_max=str(caps_raw.get("mem_limit_max", _DEFAULT_MEM_LIMIT_MAX)),
            pids_limit_max=int(caps_raw.get("pids_limit_max", _DEFAULT_PIDS_LIMIT_MAX)),
            wall_timeout_max=int(caps_raw.get("wall_timeout_max", _DEFAULT_WALL_TIMEOUT_MAX)),
        )
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"Invalid caps section: {exc}") from exc

    registry = Registry(
        approved_images=frozenset(approved_images),
        allowed_networks=frozenset(allowed_networks),
        allowed_volume_names=frozenset(allowed_volume_names),
        caps=caps,
    )

    logger.info(
        "Worker proxy registry loaded from %s: approved_images=%s, "
        "allowed_networks=%s, allowed_volume_names=%s, caps=%s",
        path,
        sorted(registry.approved_images),
        sorted(registry.allowed_networks),
        sorted(registry.allowed_volume_names),
        registry.caps,
    )
    return registry


# Module-level active registry. main.py sets this at startup; validators
# in models.py read it to enforce the allowlist.
_active: Optional[Registry] = None


def set_active(registry: Registry) -> None:
    global _active
    _active = registry


def get_active() -> Registry:
    """Return the active registry. Raises if startup didn't set one."""
    if _active is None:
        raise RuntimeError("Worker proxy registry not initialized")
    return _active


def parse_mem_bytes(value: str) -> int:
    """Parse a Docker mem_limit string like "256m", "4g" into bytes.

    Raises ValueError on malformed input.
    """
    v = value.strip().lower()
    if not v:
        raise ValueError(f"empty mem_limit")
    multipliers = {"k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}
    if v[-1] in multipliers:
        return int(v[:-1]) * multipliers[v[-1]]
    if v[-1].isdigit():
        return int(v)
    raise ValueError(f"unsupported mem_limit suffix in {value!r}")
