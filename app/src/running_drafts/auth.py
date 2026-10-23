import logging
import sqlite3
from typing import Annotated
from uuid import UUID

from fastapi import Request, Security
from fastapi.security import APIKeyCookie

from running_drafts.application_sessions import resolve_application_session
from running_drafts.database import DatabaseConfigurationError
from running_drafts.problems import ProblemException
from running_drafts.session_cookie import SESSION_COOKIE_NAME
from running_drafts.settings import Settings

logger = logging.getLogger(__name__)
session_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="ApplicationSession",
    auto_error=False,
)


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
    try:
        session = resolve_application_session(settings, session_token)
    except (DatabaseConfigurationError, OSError, sqlite3.Error):
        logger.exception("application session lookup failed")
        session = None
    if session is None:
        raise _authentication_required("Session")
    return session.account_id
