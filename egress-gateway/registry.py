"""Egress registry loader and validator."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class EgressRoute:
    """A single route from the egress registry."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.route_id: str = data["route_id"]
        self.provider: str = data["provider"]
        self.endpoint_url: str = data["endpoint_url"]
        self.model_string: str = data["model_string"]
        self.allowed_capabilities: list[str] = data["allowed_capabilities"]
        self.auth_method: str = data["auth"]["method"]
        self.secret_ref: Optional[str] = data["auth"].get("secret_ref")
        self.rate_limit_rpm: int = data["constraints"]["rate_limit_rpm"]
        self.timeout_ms: int = data["health"]["timeout_ms"]
        self.enabled: bool = data["enabled"]

    def matches_model(self, target_model: str) -> bool:
        """Check if target_model matches the route's model_string glob."""
        return fnmatch.fnmatch(target_model, self.model_string)


class EgressRegistry:
    """Loads and manages the egress route registry."""

    def __init__(self, config_path: str = "/var/drnt/config/egress.json") -> None:
        self._config_path = config_path
        self._routes: dict[str, EgressRoute] = {}
        self._config_hash: str = ""

    def load(self) -> None:
        """Load routes from the config file."""
        with open(self._config_path, "r") as f:
            raw = f.read()

        self._config_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        data = json.loads(raw)

        self._routes = {}
        for route_data in data.get("routes", []):
            route = EgressRoute(route_data)
            self._routes[route.route_id] = route

        logger.info(
            "Loaded %d egress routes, config_hash=%s...",
            len(self._routes),
            self._config_hash[:16],
        )

    @property
    def config_hash(self) -> str:
        return self._config_hash

    def get_route(self, route_id: str) -> Optional[EgressRoute]:
        return self._routes.get(route_id)

    def all_routes(self) -> list[EgressRoute]:
        return list(self._routes.values())
