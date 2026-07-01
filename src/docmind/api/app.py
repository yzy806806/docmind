"""FastAPI application factory with middleware stack."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from docmind.api.routes import router
from docmind.config import settings
from docmind.core.correlation import (
    generate_correlation_id,
    is_valid_correlation_id,
    set_correlation_id,
)
from docmind.errors import DocMindError
from docmind.observability.features import is_feature_enabled
from docmind.observability.health import get_health, get_liveness, get_readiness
from docmind.observability.logging import configure_logging, get_logger
from docmind.observability.telemetry import configure_telemetry

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifecycle: startup and shutdown."""
    configure_logging()
    configure_telemetry()
    logger.info("docmind_starting", version="0.4.0")
    yield
    logger.info("docmind_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="DocMind",
        description="Document Intelligence Platform",
        version="0.4.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Correlation ID middleware
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = request.headers.get(settings.correlation_id_header)
        if not cid or not is_valid_correlation_id(cid):
            cid = generate_correlation_id()
        set_correlation_id(cid)

        response: Response = await call_next(request)
        response.headers[settings.correlation_id_header] = cid
        return response

    # Error handling
    @app.exception_handler(DocMindError)
    async def docmind_error_handler(request: Request, exc: DocMindError) -> JSONResponse:
        status_map = {
            "NOT_FOUND": 404,
            "FORBIDDEN": 403,
            "UNAUTHORIZED": 401,
            "VALIDATION_ERROR": 422,
            "CONFLICT": 409,
            "RATE_LIMITED": 429,
            "FEATURE_DISABLED": 503,
            "SERVICE_UNAVAILABLE": 503,
        }
        status = status_map.get(exc.code.value, 500)
        return JSONResponse(
            status_code=status,
            content={
                "error": exc.code.value,
                "message": str(exc),
                "details": exc.details,
            },
        )

    # Feature flag middleware for multi-user endpoints
    @app.middleware("http")
    async def feature_flag_middleware(request: Request, call_next):
        # If route starts with multi-user paths but feature is off, block early
        if request.url.path.startswith(("/api/tenants", "/api/teams")) and not is_feature_enabled("multi_user"):
            return JSONResponse(
                status_code=503,
                content={
                    "error": "FEATURE_DISABLED",
                    "message": "Multi-user features are not enabled",
                },
            )
        return await call_next(request)

    # Routes
    app.include_router(router)

    # Health check endpoints
    @app.get("/health")
    async def health():
        return await get_health()

    @app.get("/health/live")
    async def live():
        return await get_liveness()

    @app.get("/health/ready")
    async def ready():
        return await get_readiness()

    return app


app = create_app()
