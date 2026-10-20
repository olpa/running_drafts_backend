import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def run_command(command: str, database_path: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": "test",
        "DATABASE_PATH": str(database_path),
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_migrate_creates_the_database_at_the_schema_head(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"

    first = run_command("migrate", database_path)
    second = run_command("migrate", database_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["event"] == "database_migrated"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (2,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        child_columns = {
            table: {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("sections", "audio_assets", "transport_frames")
        }
    assert {
        "users",
        "basic_auth_identities",
        "projects",
        "sections",
        "audio_assets",
        "transport_frames",
        "provider_identities",
        "application_sessions",
        "oidc_login_attempts",
    } <= tables
    assert "documents" not in tables
    assert all("owner_id" not in columns for columns in child_columns.values())


def test_oidc_migration_advances_an_existing_schema_without_losing_accounts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    account_id = "00000000-0000-4000-8000-000000000001"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (id, created_at_ms) VALUES (?, ?)",
            (account_id, 1),
        )
        connection.execute("DROP TABLE oidc_login_attempts")
        connection.execute("DROP TABLE application_sessions")
        connection.execute("DROP TABLE provider_identities")
        connection.execute("PRAGMA user_version = 2")

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("SELECT id FROM users").fetchone() == (account_id,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert {"provider_identities", "application_sessions", "oidc_login_attempts"} <= tables
