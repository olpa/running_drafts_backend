import logging
import secrets
import sqlite3
from typing import Annotated
from uuid import UUID

from fastapi import Request, Security
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from running_drafts.database import DatabaseConfigurationError, existing_connection
from running_drafts.problems import ProblemException
from running_drafts.settings import AuthMode, Settings

logger = logging.getLogger(__name__)
basic_scheme = HTTPBasic(auto_error=False)


def ensure_authentication_adapter_is_available(settings: Settings) -> None:
    if settings.auth_mode is AuthMode.AUTH0:
        raise RuntimeError("AUTH_MODE=auth0 is not implemented")


def _authentication_required() -> ProblemException:
    return ProblemException(
        status=401,
        title="Authentication required",
        detail="Valid credentials are required.",
        code="authentication_required",
        headers={"WWW-Authenticate": "Basic"},
    )


def authenticated_user_id(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Security(basic_scheme)],
) -> UUID:
    settings: Settings = request.app.state.settings
    supplied_username = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    username_is_configured = any(
        secrets.compare_digest(supplied_username, configured)
        for configured in settings.basic_auth_usernames
    )
    password_is_valid = secrets.compare_digest(
        supplied_password,
        settings.basic_auth_password.get_secret_value(),
    )
    if credentials is None or not username_is_configured or not password_is_valid:
        raise _authentication_required()

    try:
        with existing_connection(
            settings.database_path,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        ) as connection:
            row = connection.execute(
                "SELECT user_id FROM basic_auth_identities WHERE username = ?",
                (supplied_username,),
            ).fetchone()
    except DatabaseConfigurationError, OSError, sqlite3.Error:
        logger.exception("configured Basic identity lookup failed")
        row = None
    if row is None:
        logger.error("configured Basic identity has no fixture row")
        raise ProblemException(
            status=500,
            title="Internal configuration error",
            detail="The application authentication configuration is invalid.",
            code="internal_configuration_error",
        )
    try:
        return UUID(row[0])
    except TypeError, ValueError:
        logger.exception("configured Basic identity has an invalid user UUID")
        raise ProblemException(
            status=500,
            title="Internal configuration error",
            detail="The application authentication configuration is invalid.",
            code="internal_configuration_error",
        ) from None
