import base64
import hashlib
import hmac
import logging
import secrets
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request, Response

from running_drafts import clock
from running_drafts.application_sessions import (
    IssuedApplicationSession,
    insert_application_session,
)
from running_drafts.database import (
    DatabaseBusy,
    DatabaseConfigurationError,
    DatabaseUnavailable,
    existing_connection,
    is_busy_error,
)
from running_drafts.models import CamelCaseResponse
from running_drafts.problems import ProblemException
from running_drafts.session_cookie import set_session_cookie
from running_drafts.settings import Settings

logger = logging.getLogger(__name__)

CONTROL_PREFIX = "/api/v1/test-control"
CONTROL_HEADER = "X-Test-Control"
DERIVATION = "hmac-sha256"
DERIVATION_CONTEXT = "running-drafts:test-control:v1:"
FIXTURE_ACCOUNT_ID = UUID("4316a559-d21f-5ee7-8a0a-9d09039f79ba")
FIXTURE_PROVIDER = "fixture"
FIXTURE_ISSUER = "urn:running-drafts:test-control"
FIXTURE_SUBJECT = "primary"


class FixtureOwnershipConflict(Exception):
    """The reserved fixture identifiers do not describe the fixture Account."""


class ChallengeResponse(CamelCaseResponse):
    challenge: str
    derivation: str
    context: str


class ResetResponse(CamelCaseResponse):
    status: str


class CreateResponse(CamelCaseResponse):
    status: str
    account_id: UUID
    session_expires_at: datetime


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def derive_control_token(derivation_key: str, challenge: bytes) -> str:
    digest = hmac.new(
        derivation_key.encode("utf-8"),
        DERIVATION_CONTEXT.encode("ascii") + challenge,
        hashlib.sha256,
    ).digest()
    return _base64url(digest)


def _bearer_token(header: str | None) -> str:
    if header is None:
        return ""
    scheme, separator, token = header.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        return ""
    return token


def _authentication_required(scheme: str) -> ProblemException:
    return ProblemException(
        status=401,
        title="Authentication required",
        detail="Valid credentials are required.",
        code="authentication_required",
        headers={"WWW-Authenticate": scheme},
    )


def _log_attempt(
    operation: str,
    outcome: str,
    *,
    level: int = logging.INFO,
    session_expires_at: datetime | None = None,
    reference_id: str | None = None,
) -> None:
    extra: dict[str, object] = {"operation": operation, "outcome": outcome}
    if operation in {"reset", "create"} and outcome == "succeeded":
        extra["accountId"] = str(FIXTURE_ACCOUNT_ID)
    if session_expires_at is not None:
        extra["sessionExpiresAt"] = session_expires_at.isoformat()
    if reference_id is not None:
        extra["referenceId"] = reference_id
    logger.log(level, "test control attempt", extra=extra)


def _ownership_state(connection: sqlite3.Connection) -> bool:
    user_exists = (
        connection.execute(
            "SELECT 1 FROM users WHERE id = ?",
            (str(FIXTURE_ACCOUNT_ID),),
        ).fetchone()
        is not None
    )
    account_identity = connection.execute(
        """
        SELECT issuer, subject
        FROM provider_identities
        WHERE account_id = ?
        """,
        (str(FIXTURE_ACCOUNT_ID),),
    ).fetchone()
    canonical_identity = connection.execute(
        """
        SELECT account_id
        FROM provider_identities
        WHERE issuer = ? AND subject = ?
        """,
        (FIXTURE_ISSUER, FIXTURE_SUBJECT),
    ).fetchone()

    if not user_exists and account_identity is None and canonical_identity is None:
        return False
    if (
        user_exists
        and account_identity == (FIXTURE_ISSUER, FIXTURE_SUBJECT)
        and canonical_identity == (str(FIXTURE_ACCOUNT_ID),)
    ):
        return True
    raise FixtureOwnershipConflict


def _database_failure(error: BaseException) -> BaseException:
    if isinstance(error, sqlite3.OperationalError) and is_busy_error(error):
        return DatabaseBusy()
    if isinstance(error, (DatabaseConfigurationError, OSError, sqlite3.DatabaseError)):
        return DatabaseUnavailable()
    return error


def reset_fixture(settings: Settings) -> None:
    try:
        with existing_connection(
            settings.database_path,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if _ownership_state(connection):
                    connection.execute(
                        "DELETE FROM users WHERE id = ?",
                        (str(FIXTURE_ACCOUNT_ID),),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except FixtureOwnershipConflict:
        raise
    except BaseException as error:
        mapped = _database_failure(error)
        if mapped is error:
            raise
        raise mapped from error


def create_fixture(settings: Settings) -> IssuedApplicationSession:
    created_at_ms = clock.utc_milliseconds()
    try:
        with existing_connection(
            settings.database_path,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = _ownership_state(connection)
                if not exists:
                    connection.execute(
                        "INSERT INTO users (id, created_at_ms) VALUES (?, ?)",
                        (str(FIXTURE_ACCOUNT_ID), created_at_ms),
                    )
                    connection.execute(
                        """
                        INSERT INTO provider_identities (
                            issuer, subject, account_id, provider, email, email_verified,
                            display_name, given_name, family_name, picture_url, locale,
                            microsoft_tenant_id, first_login_at_ms, last_login_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            FIXTURE_ISSUER,
                            FIXTURE_SUBJECT,
                            str(FIXTURE_ACCOUNT_ID),
                            FIXTURE_PROVIDER,
                            "fixture@example.invalid",
                            1,
                            "Fixture User",
                            "Fixture",
                            "User",
                            None,
                            "en",
                            None,
                            created_at_ms,
                            created_at_ms,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE provider_identities
                        SET provider = ?, email = ?, email_verified = ?, display_name = ?,
                            given_name = ?, family_name = ?, picture_url = ?, locale = ?,
                            microsoft_tenant_id = ?, last_login_at_ms = MAX(last_login_at_ms, ?)
                        WHERE account_id = ?
                        """,
                        (
                            FIXTURE_PROVIDER,
                            "fixture@example.invalid",
                            1,
                            "Fixture User",
                            "Fixture",
                            "User",
                            None,
                            "en",
                            None,
                            created_at_ms,
                            str(FIXTURE_ACCOUNT_ID),
                        ),
                    )
                issued = insert_application_session(
                    connection,
                    FIXTURE_ACCOUNT_ID,
                    created_at_ms=created_at_ms,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except FixtureOwnershipConflict:
        raise
    except BaseException as error:
        mapped = _database_failure(error)
        if mapped is error:
            raise
        raise mapped from error
    return issued


def create_test_control_router(settings: Settings) -> APIRouter:
    if not settings.test_control_enabled:
        raise ValueError("test control settings are incomplete")
    bootstrap = settings.test_control_bootstrap_credential
    derivation_key = settings.test_control_derivation_key
    if bootstrap is None or derivation_key is None:
        raise AssertionError("enabled test control has no credentials")

    challenge = secrets.token_bytes(32)
    expected_bootstrap = bootstrap.get_secret_value()
    expected_control = derive_control_token(derivation_key.get_secret_value(), challenge)
    router = APIRouter(prefix=CONTROL_PREFIX, tags=["test-control"])

    def require_bootstrap(request: Request) -> None:
        supplied = _bearer_token(request.headers.get("Authorization"))
        if not secrets.compare_digest(
            supplied.encode("utf-8"),
            expected_bootstrap.encode("utf-8"),
        ):
            _log_attempt("challenge", "unauthorized", level=logging.WARNING)
            raise _authentication_required("Bearer")

    def require_control(request: Request) -> None:
        supplied = request.headers.get(CONTROL_HEADER, "")
        if not secrets.compare_digest(
            supplied.encode("utf-8"),
            expected_control.encode("ascii"),
        ):
            operation = request.url.path.rsplit("/", 1)[-1]
            _log_attempt(operation, "unauthorized", level=logging.WARNING)
            raise _authentication_required("TestControl")

    def operation_failure(operation: str, error: Exception) -> ProblemException | None:
        if isinstance(error, FixtureOwnershipConflict):
            _log_attempt(operation, "failed", level=logging.WARNING)
            return ProblemException(
                status=409,
                title="Fixture ownership conflict",
                detail="The reserved fixture identity conflicts with existing data.",
                code="fixture_ownership_conflict",
            )
        if isinstance(error, (DatabaseBusy, DatabaseUnavailable)):
            _log_attempt(operation, "failed", level=logging.WARNING)
            return None
        reference_id = str(uuid4())
        _log_attempt(
            operation,
            "failed",
            level=logging.ERROR,
            reference_id=reference_id,
        )
        return ProblemException(
            status=500,
            title="Internal server error",
            detail="The server could not complete the request.",
            code="internal_error",
            extensions={"referenceId": reference_id},
        )

    @router.get("/challenge", response_model=ChallengeResponse)
    def get_challenge(
        response: Response,
        _authorized: None = Depends(require_bootstrap),
    ) -> ChallengeResponse:
        response.headers["Cache-Control"] = "no-store"
        _log_attempt("challenge", "succeeded")
        return ChallengeResponse(
            challenge=_base64url(challenge),
            derivation=DERIVATION,
            context=DERIVATION_CONTEXT,
        )

    @router.post("/reset", response_model=ResetResponse)
    def reset(_authorized: None = Depends(require_control)) -> ResetResponse:
        try:
            reset_fixture(settings)
        except Exception as error:
            problem = operation_failure("reset", error)
            if problem is not None:
                raise problem from error
            raise
        _log_attempt("reset", "succeeded")
        return ResetResponse(status="reset")

    @router.post("/create", response_model=CreateResponse)
    def create(
        response: Response,
        _authorized: None = Depends(require_control),
    ) -> CreateResponse:
        try:
            issued = create_fixture(settings)
        except Exception as error:
            problem = operation_failure("create", error)
            if problem is not None:
                raise problem from error
            raise
        expires = datetime.fromtimestamp(issued.expires_at_ms / 1_000, tz=UTC)
        set_session_cookie(response, settings, issued.token, expires)
        _log_attempt("create", "succeeded", session_expires_at=expires)
        return CreateResponse(
            status="created",
            account_id=FIXTURE_ACCOUNT_ID,
            session_expires_at=expires,
        )

    return router
