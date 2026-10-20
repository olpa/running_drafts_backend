import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from uuid import UUID

from running_drafts import clock
from running_drafts.database import existing_connection
from running_drafts.settings import Settings

SESSION_LIFETIME_MS = 14 * 24 * 60 * 60 * 1_000


@dataclass(frozen=True)
class ApplicationSession:
    account_id: UUID
    expires_at_ms: int


@dataclass(frozen=True)
class IssuedApplicationSession(ApplicationSession):
    token: str


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def resolve_application_session(
    settings: Settings,
    token: str | None,
    *,
    now_ms: int | None = None,
) -> ApplicationSession | None:
    if not token:
        return None
    effective_now = clock.utc_milliseconds() if now_ms is None else now_ms
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        row = connection.execute(
            """
            SELECT account_id, expires_at_ms
            FROM application_sessions
            WHERE token_digest = ? AND expires_at_ms > ?
            """,
            (_digest(token), effective_now),
        ).fetchone()
    if row is None:
        return None
    try:
        return ApplicationSession(account_id=UUID(row[0]), expires_at_ms=row[1])
    except (TypeError, ValueError):
        return None


def replace_application_session(
    settings: Settings,
    account_id: UUID,
    previous_token: str | None,
    *,
    now_ms: int | None = None,
) -> IssuedApplicationSession:
    created_at_ms = clock.utc_milliseconds() if now_ms is None else now_ms
    expires_at_ms = created_at_ms + SESSION_LIFETIME_MS
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if previous_token:
                connection.execute(
                    "DELETE FROM application_sessions WHERE token_digest = ?",
                    (_digest(previous_token),),
                )
            connection.execute(
                "DELETE FROM application_sessions WHERE expires_at_ms <= ?",
                (created_at_ms,),
            )
            while True:
                token = secrets.token_urlsafe(32)
                try:
                    connection.execute(
                        """
                        INSERT INTO application_sessions (
                            token_digest, account_id, created_at_ms, expires_at_ms
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (_digest(token), str(account_id), created_at_ms, expires_at_ms),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return IssuedApplicationSession(
        token=token,
        account_id=account_id,
        expires_at_ms=expires_at_ms,
    )


def end_application_session(settings: Settings, token: str | None) -> None:
    if not token:
        return
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute(
            "DELETE FROM application_sessions WHERE token_digest = ?",
            (_digest(token),),
        )
