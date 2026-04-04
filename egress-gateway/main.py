"""DRNT Egress Gateway — Phase 3.

Validates dispatch requests against the egress registry and forwards
them to cloud APIs. Sits on both drnt-internal and drnt-external networks.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
import uuid_utils
from dotenv import dotenv_values
from fastapi import FastAPI
from pydantic import BaseModel

from providers import ADAPTERS
from rate_limiter import SlidingWindowRateLimiter
from registry import EgressRegistry

logging.basicConfig(
    level=os.environ.get("DRNT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SECRETS_PATH = os.environ.get("DRNT_SECRETS_PATH", "/var/drnt/secrets/.env")
CONFIG_PATH = os.environ.get("DRNT_CONFIG_PATH", "/var/drnt/config/egress.json")

registry = EgressRegistry(config_path=CONFIG_PATH)
rate_limiter = SlidingWindowRateLimiter()
secrets: dict[str, str | None] = {}


class DispatchRequest(BaseModel):
    job_id: str
    route_id: str
    capability_id: str
    target_model: str
    prompt: str
    assembled_payload_hash: str
    wal_permission_check_ref: str


class DispatchResponse(BaseModel):
    status: str
    model: Optional[str] = None
    response_text: Optional[str] = None
    token_count_in: Optional[int] = None
    token_count_out: Optional[int] = None
    cost_estimate_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    result_id: Optional[str] = None
    response_hash: Optional[str] = None
    finish_reason: Optional[str] = None
    egress_config_hash: Optional[str] = None
    # Failure fields
    failure_type: Optional[str] = None
    detail: Optional[str] = None


def _load_secrets() -> dict[str, str | None]:
    """Load API keys from the secrets .env file."""
    if os.path.exists(SECRETS_PATH):
        return dotenv_values(SECRETS_PATH)
    logger.warning("Secrets file not found at %s", SECRETS_PATH)
    return {}


def _estimate_cost(provider: str, tokens_in: int, tokens_out: int) -> float:
    """Rough cost estimate per provider."""
    rates = {
        "anthropic": (3.0 / 1_000_000, 15.0 / 1_000_000),
        "openai": (2.5 / 1_000_000, 10.0 / 1_000_000),
        "google": (0.075 / 1_000_000, 0.30 / 1_000_000),
        "ollama": (0.0, 0.0),
    }
    in_rate, out_rate = rates.get(provider, (0.0, 0.0))
    return round(tokens_in * in_rate + tokens_out * out_rate, 6)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global secrets
    registry.load()
    secrets = _load_secrets()
    logger.info("Egress gateway ready, %d secrets loaded", len(secrets))
    yield


app = FastAPI(title="DRNT Egress Gateway", version="0.3.0", lifespan=lifespan)


@app.post("/dispatch", response_model=DispatchResponse)
async def dispatch(req: DispatchRequest):
    """Execute the Spec 4 check sequence and dispatch to cloud API."""

    # Check 1: Route exists
    route = registry.get_route(req.route_id)
    if route is None:
        return DispatchResponse(
            status="blocked",
            failure_type="route_mismatch",
            detail=f"Route '{req.route_id}' not found in egress registry",
        )

    # Check 2: Route enabled
    if not route.enabled:
        return DispatchResponse(
            status="blocked",
            failure_type="policy_violation",
            detail=f"Route '{req.route_id}' is disabled",
        )

    # Check 3: Capability binding
    if req.capability_id not in route.allowed_capabilities:
        return DispatchResponse(
            status="blocked",
            failure_type="route_mismatch",
            detail=f"Capability '{req.capability_id}' not allowed on route '{req.route_id}'",
        )

    # Check 4: Model binding
    if not route.matches_model(req.target_model):
        return DispatchResponse(
            status="blocked",
            failure_type="model_mismatch",
            detail=f"Model '{req.target_model}' does not match route pattern '{route.model_string}'",
        )

    # Check 5: Rate limit
    if not rate_limiter.check(req.route_id, route.rate_limit_rpm):
        count = rate_limiter.current_count(req.route_id)
        return DispatchResponse(
            status="blocked",
            failure_type="policy_violation",
            detail=f"Rate limit exceeded for route {req.route_id} ({route.rate_limit_rpm} RPM, current: {count})",
        )

    # Check 6: Auth resolution
    api_key = ""
    if route.auth_method != "none":
        secret_ref = route.secret_ref
        if not secret_ref:
            return DispatchResponse(
                status="blocked",
                failure_type="endpoint_unavailable",
                detail=f"No secret_ref configured for route '{req.route_id}'",
            )
        api_key = secrets.get(secret_ref, "") or ""
        if not api_key:
            return DispatchResponse(
                status="blocked",
                failure_type="endpoint_unavailable",
                detail=f"Secret '{secret_ref}' not set or empty",
            )

    # Build provider-specific request
    adapter_cls = ADAPTERS.get(route.provider)
    if adapter_cls is None:
        return DispatchResponse(
            status="blocked",
            failure_type="route_mismatch",
            detail=f"Unknown provider '{route.provider}'",
        )

    url, headers, body = adapter_cls.build_request(
        endpoint_url=route.endpoint_url,
        model=req.target_model,
        prompt=req.prompt,
        api_key=api_key,
    )

    # Check 7: TLS / connectivity — make the actual call
    start_ms = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=route.timeout_ms / 1000.0) as client:
            resp = await client.post(url, headers=headers, json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        latency = int((time.monotonic() - start_ms) * 1000)
        return DispatchResponse(
            status="error",
            model=req.target_model,
            failure_type="endpoint_unavailable",
            detail=f"Connection failed: {exc}",
            latency_ms=latency,
        )
    except httpx.TimeoutException as exc:
        latency = int((time.monotonic() - start_ms) * 1000)
        return DispatchResponse(
            status="error",
            model=req.target_model,
            failure_type="endpoint_unavailable",
            detail=f"Request timed out: {exc}",
            latency_ms=latency,
        )

    latency_ms = int((time.monotonic() - start_ms) * 1000)

    # Handle HTTP errors
    if resp.status_code >= 400:
        return DispatchResponse(
            status="error",
            model=req.target_model,
            failure_type="endpoint_unavailable",
            detail=f"HTTP {resp.status_code}: {resp.text[:500]}",
            latency_ms=latency_ms,
        )

    # Parse response
    try:
        data = resp.json()
        parsed = adapter_cls.parse_response(data)
    except Exception as exc:
        return DispatchResponse(
            status="error",
            model=req.target_model,
            failure_type="endpoint_unavailable",
            detail=f"Response parse error: {exc}",
            latency_ms=latency_ms,
        )

    response_text = parsed["response_text"]
    token_in = parsed["token_count_in"]
    token_out = parsed["token_count_out"]
    cost = _estimate_cost(route.provider, token_in, token_out)
    result_id = str(uuid_utils.uuid7())
    response_hash = hashlib.sha256(response_text.encode("utf-8")).hexdigest()

    return DispatchResponse(
        status="ok",
        model=req.target_model,
        response_text=response_text,
        token_count_in=token_in,
        token_count_out=token_out,
        cost_estimate_usd=cost,
        latency_ms=latency_ms,
        result_id=result_id,
        response_hash=response_hash,
        finish_reason=parsed["finish_reason"],
        egress_config_hash=registry.config_hash,
    )


@app.get("/health")
async def health():
    return {
        "status": "running",
        "routes_loaded": len(registry.all_routes()),
        "config_hash": registry.config_hash[:16] if registry.config_hash else None,
    }
