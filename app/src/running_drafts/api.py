import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from running_drafts.auth import SESSION_COOKIE_NAME
from running_drafts.auth_routes import router as auth_router
from running_drafts.database import (
    DatabaseBusy,
    DatabaseConfigurationError,
    DatabaseUnavailable,
    existing_connection,
)
from running_drafts.migrations import MIGRATION_HEAD
from running_drafts.oauth import ProviderRegistry, create_provider_registry
from running_drafts.problems import ProblemException, problem_response
from running_drafts.project_api import (
    ConcurrentDownloadLimitExceeded,
    DownloadCapacity,
)
from running_drafts.project_api import router as project_router
from running_drafts.project_storage import (
    AudioAssemblyFailure,
    AudioAssetNotSealed,
    AudioAssetSealConflict,
    DownloadStorageUnavailable,
    FrameConflict,
    IdempotencyKeyReused,
    ResourceNotFound,
)
from running_drafts.settings import AppEnvironment, Settings
from running_drafts.test_control import CONTROL_PREFIX, create_test_control_router

logger = logging.getLogger(__name__)


class SchemaVersionMismatch(RuntimeError):
    pass


class CookieOriginMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            if (
                request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
                and not request.url.path.startswith(f"{CONTROL_PREFIX}/")
                and SESSION_COOKIE_NAME in request.cookies
                and request.headers.get("Origin") not in self.allowed_origins
            ):
                response = problem_response(
                    status=403,
                    title="Cross-site request rejected",
                    detail="The request origin is not allowed.",
                    code="csrf_rejected",
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _database_is_ready(settings: Settings) -> None:
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute("SELECT 1").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != MIGRATION_HEAD:
            raise SchemaVersionMismatch(
                f"database schema version is {version}, expected {MIGRATION_HEAD}"
            )


def create_app(
    settings: Settings,
    *,
    provider_registry: ProviderRegistry | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    documentation_url = "/docs" if settings.expose_api_documentation else None
    openapi_url = "/openapi.json" if settings.expose_api_documentation else None
    app = FastAPI(
        docs_url=documentation_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.provider_registry = provider_registry or create_provider_registry(settings)
    app.state.download_capacity = DownloadCapacity(settings.max_staged_download_files)
    if settings.test_control_enabled:
        app.include_router(create_test_control_router(settings))
        logger.info("test control enabled")
    else:
        absent = []
        if settings.test_control_bootstrap_credential is None:
            absent.append("TEST_CONTROL_BOOTSTRAP_CREDENTIAL")
        if settings.test_control_derivation_key is None:
            absent.append("TEST_CONTROL_DERIVATION_KEY")
        logger.warning("test control disabled missing_settings=%s", ",".join(absent))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
    app.add_middleware(CookieOriginMiddleware, allowed_origins=settings.allowed_origins)

    @app.exception_handler(ProblemException)
    def handle_problem(_request: Request, error: ProblemException) -> JSONResponse:
        return problem_response(
            status=error.status,
            title=error.title,
            detail=error.detail,
            code=error.code,
            extensions=error.extensions,
            headers=error.headers,
        )

    @app.exception_handler(ResourceNotFound)
    def handle_not_found(_request: Request, _error: ResourceNotFound) -> JSONResponse:
        return problem_response(
            status=404,
            title="Resource not found",
            detail="The requested resource was not found.",
            code="resource_not_found",
        )

    @app.exception_handler(IdempotencyKeyReused)
    def handle_idempotency_reuse(
        _request: Request,
        _error: IdempotencyKeyReused,
    ) -> JSONResponse:
        return problem_response(
            status=409,
            title="Idempotency key reused",
            detail="The idempotency key was already used with different input.",
            code="idempotency_key_reused",
        )

    @app.exception_handler(AudioAssetSealConflict)
    def handle_seal_conflict(
        _request: Request,
        _error: AudioAssetSealConflict,
    ) -> JSONResponse:
        return problem_response(
            status=409,
            title="Audio asset seal conflict",
            detail="The audio asset cannot be sealed with this command.",
            code="audio_asset_seal_conflict",
        )

    @app.exception_handler(AudioAssetNotSealed)
    def handle_asset_not_sealed(
        _request: Request,
        _error: AudioAssetNotSealed,
    ) -> JSONResponse:
        return problem_response(
            status=409,
            title="Audio asset not sealed",
            detail="The audio asset must be sealed before it can be downloaded.",
            code="asset_not_sealed",
        )

    @app.exception_handler(AudioAssemblyFailure)
    def handle_audio_assembly_failure(
        _request: Request,
        error: AudioAssemblyFailure,
    ) -> JSONResponse:
        logger.error(
            "audio assembly failed: audio_asset_id=%s expected_frame_count=%s "
            "actual_frame_count=%s expected_bytes=%s actual_bytes=%s "
            "offending_sequence=%s nearby_hashes=%s",
            error.audio_asset_id,
            error.expected_frame_count,
            error.actual_frame_count,
            error.expected_bytes,
            error.actual_bytes,
            error.offending_sequence,
            error.nearby_hashes,
        )
        return problem_response(
            status=500,
            title="Audio assembly failed",
            detail="The stored audio could not be assembled.",
            code="audio_assembly_failed",
        )

    @app.exception_handler(FrameConflict)
    def handle_frame_conflict(_request: Request, _error: FrameConflict) -> JSONResponse:
        return problem_response(
            status=409,
            title="Audio frame conflict",
            detail="The audio frame conflicts with the current asset state.",
            code="frame_conflict",
        )

    @app.exception_handler(DatabaseBusy)
    def handle_database_busy(_request: Request, _error: DatabaseBusy) -> JSONResponse:
        return problem_response(
            status=503,
            title="Database busy",
            detail="The database is busy. The request may be retried.",
            code="database_busy",
        )

    @app.exception_handler(DatabaseUnavailable)
    def handle_database_unavailable(
        _request: Request,
        _error: DatabaseUnavailable,
    ) -> JSONResponse:
        return problem_response(
            status=503,
            title="Database unavailable",
            detail="The database is unavailable.",
            code="database_unavailable",
        )

    @app.exception_handler(DownloadStorageUnavailable)
    def handle_download_storage_unavailable(
        _request: Request,
        _error: DownloadStorageUnavailable,
    ) -> JSONResponse:
        return problem_response(
            status=503,
            title="Storage unavailable",
            detail="Temporary storage is unavailable.",
            code="storage_unavailable",
        )

    @app.exception_handler(ConcurrentDownloadLimitExceeded)
    def handle_concurrent_download_limit(
        _request: Request,
        _error: ConcurrentDownloadLimitExceeded,
    ) -> JSONResponse:
        return problem_response(
            status=429,
            title="Concurrent download limit exceeded",
            detail="The service is already staging the maximum number of downloads.",
            code="concurrent_download_limit_exceeded",
        )

    @app.exception_handler(RequestValidationError)
    def handle_validation(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        extensions = None
        if settings.app_env is not AppEnvironment.PRODUCTION:
            extensions = {
                "errors": [
                    {
                        "location": list(item["loc"]),
                        "message": item["msg"],
                        "type": item["type"],
                    }
                    for item in error.errors()
                ]
            }
        return problem_response(
            status=422,
            title="Request validation failed",
            detail="The request does not match the API contract.",
            code="request_validation_error",
            extensions=extensions,
        )

    @app.exception_handler(StarletteHTTPException)
    def handle_http_error(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        return problem_response(
            status=error.status_code,
            title="HTTP error",
            detail="The request could not be completed.",
            code="http_error",
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        reference_id = str(uuid4())
        logger.error(
            "unhandled application exception referenceId=%s",
            reference_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        return problem_response(
            status=500,
            title="Internal server error",
            detail="The server could not complete the request.",
            code="internal_error",
            extensions={"referenceId": reference_id},
        )

    health = APIRouter(prefix="/health", tags=["health"])

    @health.get("/live")
    def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @health.get("/ready", response_model=None)
    def readiness() -> dict[str, str] | JSONResponse:
        try:
            _database_is_ready(settings)
        except (
            DatabaseConfigurationError,
            OSError,
            SchemaVersionMismatch,
            sqlite3.Error,
        ):
            logger.exception("database readiness check failed")
            return problem_response(
                status=503,
                title="Database not ready",
                detail="The application database is not ready.",
                code="database_not_ready",
            )
        return {"status": "ok"}

    app.include_router(health)
    app.include_router(auth_router)
    app.include_router(project_router)
    return app
