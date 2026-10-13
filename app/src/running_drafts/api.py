import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from running_drafts.auth import ensure_authentication_adapter_is_available
from running_drafts.database import DatabaseConfigurationError, existing_connection
from running_drafts.migrations import MIGRATION_HEAD
from running_drafts.problems import ProblemException, problem_response
from running_drafts.settings import AppEnvironment, Settings

logger = logging.getLogger(__name__)


class SchemaVersionMismatch(RuntimeError):
    pass


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


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ensure_authentication_adapter_is_available(settings)
        yield

    documentation_url = "/docs" if settings.expose_api_documentation else None
    openapi_url = "/openapi.json" if settings.expose_api_documentation else None
    app = FastAPI(
        docs_url=documentation_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ProblemException)
    def handle_problem(_request: Request, error: ProblemException) -> JSONResponse:
        return problem_response(
            status=error.status,
            title=error.title,
            detail=error.detail,
            code=error.code,
            headers=error.headers,
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
        logger.error(
            "unhandled application exception",
            exc_info=(type(error), error, error.__traceback__),
        )
        return problem_response(
            status=500,
            title="Internal server error",
            detail="The server could not complete the request.",
            code="internal_error",
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
    app.include_router(APIRouter(prefix="/api/v1"))
    return app
