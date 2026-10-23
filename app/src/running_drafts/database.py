import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DatabaseConfigurationError(RuntimeError):
    """The configured database cannot satisfy application invariants."""


class DatabaseBusy(Exception):
    """The database did not become writable before its configured timeout."""


class DatabaseUnavailable(Exception):
    """The database could not satisfy an application operation."""


def is_busy_error(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _existing_database_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=rw"


def configure_connection(
    connection: sqlite3.Connection,
    *,
    busy_timeout_ms: int,
    require_wal: bool,
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    if require_wal:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if journal_mode.lower() != "wal":
            raise DatabaseConfigurationError(
                f"database journal mode is {journal_mode!r}, expected 'wal'"
            )


@contextmanager
def existing_connection(
    path: Path,
    *,
    busy_timeout_ms: int,
    require_wal: bool = True,
) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        _existing_database_uri(path),
        uri=True,
        timeout=busy_timeout_ms / 1_000,
        isolation_level=None,
    )
    try:
        configure_connection(
            connection,
            busy_timeout_ms=busy_timeout_ms,
            require_wal=require_wal,
        )
        yield connection
    finally:
        connection.close()
