"""Health check endpoints and dependency probes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine

from docmind.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class HealthStatus:
    status: str  # "healthy", "degraded", "unhealthy"
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


HealthCheckFn = Callable[[], Coroutine[Any, Any, dict[str, Any]]]

_registered_checks: dict[str, HealthCheckFn] = {}


def register_health_check(name: str) -> Callable[[HealthCheckFn], HealthCheckFn]:
    """Decorator to register a health check function."""

    def decorator(fn: HealthCheckFn) -> HealthCheckFn:
        _registered_checks[name] = fn
        return fn

    return decorator


async def run_health_check(name: str, fn: HealthCheckFn, timeout: float = 5.0) -> dict[str, Any]:
    """Run a single health check with a timeout."""
    try:
        result = await asyncio.wait_for(fn(), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        logger.warning("health_check_timeout", check=name)
        return {"status": "unhealthy", "error": "timeout"}
    except Exception as e:
        logger.error("health_check_failed", check=name, error=str(e))
        return {"status": "unhealthy", "error": str(e)}


async def get_health() -> HealthStatus:
    """Run all registered health checks and return aggregate status."""
    checks: dict[str, dict[str, Any]] = {}
    overall_status = "healthy"

    for name, fn in _registered_checks.items():
        checks[name] = await run_health_check(name, fn)
        if checks[name].get("status") == "unhealthy":
            overall_status = "unhealthy"
        elif checks[name].get("status") == "degraded" and overall_status == "healthy":
            overall_status = "degraded"

    if not checks:
        overall_status = "degraded"

    return HealthStatus(status=overall_status, checks=checks)


async def get_liveness() -> dict[str, str]:
    """Simple liveness check — always returns ok if the server is running."""
    return {"status": "ok"}


async def get_readiness() -> HealthStatus:
    """Readiness check — all registered dependencies must be healthy."""
    return await get_health()
