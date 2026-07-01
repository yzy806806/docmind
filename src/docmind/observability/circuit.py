"""Circuit breaker pattern for external dependency calls.

Protects against cascading failures when upstream services (DB, parsers, etc.)
are unhealthy. Uses a simple state machine: CLOSED → OPEN → HALF_OPEN → CLOSED.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Callable, Coroutine, TypeVar

from docmind.errors import DocMindError, ErrorCode
from docmind.observability.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"          # Normal operation
    OPEN = "open"              # Failing fast
    HALF_OPEN = "half_open"    # Testing if recovery is possible


@dataclass
class CircuitStats:
    failures: int = 0
    successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    state: CircuitState = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_time = time.monotonic()

    def record_success(self) -> None:
        self.successes += 1
        self.last_success_time = time.monotonic()
        self.failures = 0

    @property
    def failure_rate(self) -> float:
        total = self.failures + self.successes
        return self.failures / total if total > 0 else 0.0


class CircuitBreaker:
    """Configurable circuit breaker for async operations."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        allowed_exceptions: tuple[type[Exception], ...] = (Exception,),
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.allowed_exceptions = allowed_exceptions

        self._stats = CircuitStats()
        self._lock = asyncio.Lock()
        self._half_open_count = 0

    @property
    def state(self) -> CircuitState:
        return self._stats.state

    @property
    def stats(self) -> CircuitStats:
        return self._stats

    async def _should_attempt(self) -> bool:
        async with self._lock:
            if self._stats.state == CircuitState.CLOSED:
                return True

            if self._stats.state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._stats.last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._stats.state = CircuitState.HALF_OPEN
                    self._half_open_count = 0
                    logger.info(
                        "circuit_half_open",
                        name=self.name,
                        elapsed=elapsed,
                    )
                    return True
                return False

            # HALF_OPEN
            if self._half_open_count < self.half_open_max_calls:
                self._half_open_count += 1
                return True
            return False

    async def _on_success(self) -> None:
        async with self._lock:
            self._stats.record_success()
            self._stats.state = CircuitState.CLOSED
            logger.info("circuit_closed", name=self.name)

    async def _on_failure(self) -> None:
        async with self._lock:
            self._stats.record_failure()
            if (
                self._stats.failures >= self.failure_threshold
                and self._stats.state != CircuitState.OPEN
            ):
                self._stats.state = CircuitState.OPEN
                logger.warning(
                    "circuit_opened",
                    name=self.name,
                    failures=self._stats.failures,
                )

    async def call(self, fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Execute fn if the circuit is closed/half-open, else raise CircuitOpenError."""
        if not await self._should_attempt():
            raise CircuitOpenError(
                f"Circuit '{self.name}' is OPEN",
                {"name": self.name, "state": self._stats.state.value},
            )

        try:
            result = await fn()
            await self._on_success()
            return result
        except self.allowed_exceptions as e:
            await self._on_failure()
            raise

    def decorate(self, func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
        """Decorator to wrap an async function with this circuit breaker."""

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await self.call(lambda: func(*args, **kwargs))

        return wrapper


class CircuitOpenError(DocMindError):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message, ErrorCode.SERVICE_UNAVAILABLE, details)


# Pre-built circuit breakers for common dependencies
db_circuit = CircuitBreaker(
    name="database",
    failure_threshold=5,
    recovery_timeout=30.0,
)

parser_circuit = CircuitBreaker(
    name="parser",
    failure_threshold=3,
    recovery_timeout=60.0,
)

storage_circuit = CircuitBreaker(
    name="storage",
    failure_threshold=3,
    recovery_timeout=30.0,
)
