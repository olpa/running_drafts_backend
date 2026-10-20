import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from running_drafts.database import configure_connection
from running_drafts.settings import DatabaseSettings


class MigrationError(RuntimeError):
    """The database cannot be migrated to the application schema head."""


MigrationAction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    apply: MigrationAction


def _execute_sql_resource(filename: str) -> MigrationAction:
    def apply(connection: sqlite3.Connection) -> None:
        path = Path(__file__).with_name("migration_steps") / filename
        buffered = ""
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            buffered += line
            if sqlite3.complete_statement(buffered):
                connection.execute(buffered)
                buffered = ""
        if buffered.strip():
            raise MigrationError(f"incomplete SQL statement in {filename}")

    return apply


MIGRATIONS = (
    Migration(1, _execute_sql_resource("0001_users.sql")),
    Migration(2, _execute_sql_resource("0002_projects.sql")),
    Migration(3, _execute_sql_resource("0003_oidc.sql")),
)
MIGRATION_HEAD = MIGRATIONS[-1].version


def migrate(settings: DatabaseSettings) -> int:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        settings.database_path,
        timeout=settings.database_busy_timeout_ms / 1_000,
        isolation_level=None,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {settings.database_busy_timeout_ms}")
        current = connection.execute("PRAGMA user_version").fetchone()[0]
        auto_vacuum = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        if current == 0:
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            auto_vacuum = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        if auto_vacuum != 2:
            raise MigrationError(
                "database does not use incremental auto-vacuum; recreate this pre-release database"
            )
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if journal_mode.lower() != "wal":
            raise MigrationError("failed to enable WAL journal mode")
        configure_connection(
            connection,
            busy_timeout_ms=settings.database_busy_timeout_ms,
            require_wal=True,
        )
        if current > MIGRATION_HEAD:
            raise MigrationError(
                f"database schema version {current} is newer than head {MIGRATION_HEAD}"
            )
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration.apply(connection)
                connection.execute(f"PRAGMA user_version = {migration.version}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return MIGRATION_HEAD
    finally:
        connection.close()
