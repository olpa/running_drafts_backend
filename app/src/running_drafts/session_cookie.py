from datetime import datetime

from fastapi import Response

from running_drafts.application_sessions import SESSION_LIFETIME_MS
from running_drafts.settings import Settings

SESSION_COOKIE_NAME = "running_drafts_session"


def set_session_cookie(
    response: Response,
    settings: Settings,
    token: str,
    expires: datetime,
) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_LIFETIME_MS // 1_000,
        expires=expires,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
