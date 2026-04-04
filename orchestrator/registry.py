"""Egress route registry — reads egress.json and exposes route metadata.

The registry is read-only from the JobManager's perspective. It loads
the egress configuration once via ``load()`` and provides route lookups
for the fallback audit check (Banked Item 3).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EgressRoute:
    """A single egress route entry."""

    route_id: str
    provider: str
    endpoint_url: str
    model_string: str
    allowed_capabilities: list[str]
    enabled: bool = True
    auth: Optional[dict] = None
    constraints: Optional[dict] = None
    health: Optional[dict] = None


class EgressRegistry:
    """Loads and serves the egress route configuration."""

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path
        self._routes: list[EgressRoute] = []

    def load(self) -> None:
        """Parse egress.json and populate the route list."""
        with open(self._config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        self._routes = []
        for entry in raw.get("routes", []):
            self._routes.append(
                EgressRoute(
                    route_id=entry["route_id"],
                    provider=entry["provider"],
                    endpoint_url=entry.get("endpoint_url", ""),
                    model_string=entry.get("model_string", "*"),
                    allowed_capabilities=entry.get("allowed_capabilities", []),
                    enabled=entry.get("enabled", True),
                    auth=entry.get("auth"),
                    constraints=entry.get("constraints"),
                    health=entry.get("health"),
                )
            )

        logger.info(
            "EgressRegistry loaded: %d routes from %s",
            len(self._routes),
            self._config_path,
        )

    def all_routes(self) -> list[EgressRoute]:
        """Return all loaded routes."""
        return list(self._routes)

    def get_route(self, route_id: str) -> Optional[EgressRoute]:
        """Look up a route by ID."""
        for r in self._routes:
            if r.route_id == route_id:
                return r
        return None
