import base64
import binascii
import logging
import secrets
import sqlite3
from typing import Annotated
from uuid import UUID

from fastapi import Request, Security
from fastapi.security import APIKeyCookie

from running_drafts.application_sessions import resolve_application_session
from running_drafts.database import DatabaseConfigurationError, existing_connection
from running_drafts.problems import ProblemException
from running_drafts.settings import AuthMode, Settings

logger = logging.getLogger(__name__)
SESSION_COOKIE_NAME = "running_drafts_session"
session_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="ApplicationSession",
    auto_error=False,
)


def ensure_authentication_adapter_is_available(settings: Settings) -> None:
    if settings.auth_mode not in {AuthMode.BASIC, AuthMode.OIDC}:
        raise RuntimeError("configured authentication mode is not implemented")


def _authentication_required(challenge: str) -> ProblemException:
    return ProblemException(
        status=401,
        title="Authentication required",
        detail="Valid credentials are required.",
        code="authentication_required",
        headers={"WWW-Authenticate": challenge},
    )


def authenticated_user_id(
    request: Request,
    session_token: Annotated[str | None, Security(session_cookie_scheme)],
) -> UUID:
    settings: Settings = request.app.state.settings
    if settings.auth_mode is AuthMode.OIDC:
        try:
            session = resolve_application_session(settings, session_token)
        except (DatabaseConfigurationError, OSError, sqlite3.Error):
            logger.exception("application session lookup failed")
            session = None
        if session is None:
            raise _authentication_required("Session")
        return session.account_id
    return _basic_authenticated_user_id(request, settings)


def _basic_authenticated_user_id(request: Request, settings: Settings) -> UUID:
    username, password = _basic_credentials(request.headers.get("Authorization"))
    username_is_configured = any(
        secrets.compare_digest(username, configured)
        for configured in settings.basic_auth_usernames
    )
    configured_password = (
        settings.basic_auth_password.get_secret_value() if settings.basic_auth_password else ""
    )
    password_is_valid = secrets.compare_digest(password, configured_password)
    if not username or not username_is_configured or not password_is_valid:
        raise _authentication_required("Basic")

    try:
        with existing_connection(
            settings.database_path,
            busy_timeout_ms=settings.database_busy_timeout_ms,
        ) as connection:
            row = connection.execute(
                "SELECT user_id FROM basic_auth_identities WHERE username = ?",
                (username,),
            ).fetchone()
    except (DatabaseConfigurationError, OSError, sqlite3.Error):
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
    except (TypeError, ValueError):
        logger.exception("configured Basic identity has an invalid user UUID")
        raise ProblemException(
            status=500,
            title="Internal configuration error",
            detail="The application authentication configuration is invalid.",
            code="internal_configuration_error",
        ) from None


def _basic_credentials(header: str | None) -> tuple[str, str]:
    if not header:
        return "", ""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return "", ""
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "", ""
    return username, password
