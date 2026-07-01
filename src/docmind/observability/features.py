"""Feature flag management.

Multi-user and other enterprise features are default-off.
Controlled via DOCMIND_FEATURE_* env vars.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from docmind.config import settings
from docmind.errors import FeatureDisabledError
from docmind.observability.logging import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def require_feature(feature_name: str) -> Callable[[F], F]:
    """Decorator that requires a feature flag to be enabled.

    Usage:
        @require_feature("multi_user")
        async def list_tenants(...):
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not getattr(settings.features, feature_name, False):
                raise FeatureDisabledError(feature_name)
            return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def is_feature_enabled(feature_name: str) -> bool:
    """Check if a feature flag is enabled."""
    return getattr(settings.features, feature_name, False)
