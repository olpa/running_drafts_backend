import hashlib
import secrets
from dataclasses import dataclass

from running_drafts import clock
from running_drafts.database import existing_connection
from running_drafts.oauth import new_pkce_verifier
from running_drafts.settings import Settings

LOGIN_ATTEMPT_LIFETIME_MS = 10 * 60 * 1_000


@dataclass(frozen=True)
class LoginAttempt:
    state: str
    provider: str
    nonce: str
    code_verifier: str
    return_to: str


def _digest(state: str) -> bytes:
    return hashlib.sha256(state.encode("ascii")).digest()


def create_login_attempt(
    settings: Settings,
    provider: str,
    return_to: str,
    *,
    now_ms: int | None = None,
) -> LoginAttempt:
    created_at_ms = clock.utc_milliseconds() if now_ms is None else now_ms
    attempt = LoginAttempt(
        state=secrets.token_urlsafe(32),
        provider=provider,
        nonce=secrets.token_urlsafe(32),
        code_verifier=new_pkce_verifier(),
        return_to=return_to,
    )
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "DELETE FROM oidc_login_attempts WHERE expires_at_ms <= ?",
                (created_at_ms,),
            )
            connection.execute(
                """
                INSERT INTO oidc_login_attempts (
                    state_digest, provider, nonce, code_verifier, return_to,
                    created_at_ms, expires_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _digest(attempt.state),
                    provider,
                    attempt.nonce,
                    attempt.code_verifier,
                    return_to,
                    created_at_ms,
                    created_at_ms + LOGIN_ATTEMPT_LIFETIME_MS,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return attempt


def consume_login_attempt(
    settings: Settings,
    state: str,
    *,
    now_ms: int | None = None,
) -> LoginAttempt | None:
    effective_now = clock.utc_milliseconds() if now_ms is None else now_ms
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT provider, nonce, code_verifier, return_to
                FROM oidc_login_attempts
                WHERE state_digest = ? AND expires_at_ms > ?
                """,
                (_digest(state), effective_now),
            ).fetchone()
            connection.execute(
                "DELETE FROM oidc_login_attempts WHERE state_digest = ?",
                (_digest(state),),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    if row is None:
        return None
    return LoginAttempt(
        state=state,
        provider=row[0],
        nonce=row[1],
        code_verifier=row[2],
        return_to=row[3],
    )


def delete_login_attempt(settings: Settings, state: str) -> None:
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute(
            "DELETE FROM oidc_login_attempts WHERE state_digest = ?",
            (_digest(state),),
        )
