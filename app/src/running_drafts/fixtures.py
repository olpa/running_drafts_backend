from datetime import UTC, datetime
from uuid import uuid4

from running_drafts.database import existing_connection
from running_drafts.migrations import MIGRATION_HEAD
from running_drafts.settings import FixtureSettings


class FixtureError(RuntimeError):
    """Development fixtures cannot be applied safely."""


def create_fixtures(settings: FixtureSettings) -> int:
    inserted = 0
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != MIGRATION_HEAD:
            raise FixtureError(f"database schema version is {version}, expected {MIGRATION_HEAD}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for username in settings.basic_auth_usernames:
                row = connection.execute(
                    "SELECT user_id FROM basic_auth_identities WHERE username = ?",
                    (username,),
                ).fetchone()
                if row is not None:
                    continue
                user_id = str(uuid4())
                created_at_ms = int(datetime.now(UTC).timestamp() * 1_000)
                connection.execute(
                    "INSERT INTO users (id, created_at_ms) VALUES (?, ?)",
                    (user_id, created_at_ms),
                )
                connection.execute(
                    "INSERT INTO basic_auth_identities (username, user_id) VALUES (?, ?)",
                    (username, user_id),
                )
                inserted += 1
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return inserted
