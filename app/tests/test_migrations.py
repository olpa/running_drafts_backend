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
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
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
        "projects",
        "sections",
        "audio_assets",
        "transport_frames",
        "provider_identities",
        "application_sessions",
        "oidc_login_attempts",
    } <= tables
    assert "basic_auth_identities" not in tables
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
        connection.execute(
            """
            CREATE TABLE basic_auth_identities (
                username TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("PRAGMA user_version = 2")

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute("SELECT id FROM users").fetchone() == (account_id,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert {"provider_identities", "application_sessions", "oidc_login_attempts"} <= tables
    assert "basic_auth_identities" not in tables


def test_basic_auth_removal_deletes_fixture_accounts_and_owned_data(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    fixture_account = "00000000-0000-4000-8000-000000000001"
    retained_account = "00000000-0000-4000-8000-000000000002"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE basic_auth_identities (
                username TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.executemany(
            "INSERT INTO users (id, created_at_ms) VALUES (?, 0)",
            [(fixture_account,), (retained_account,)],
        )
        connection.execute(
            "INSERT INTO basic_auth_identities (username, user_id) VALUES ('fixture', ?)",
            (fixture_account,),
        )
        connection.execute(
            """
            INSERT INTO projects (
                id, owner_id, creation_key, original_title, title, created_at_ms, updated_at_ms
            ) VALUES ('00000000-0000-4000-8000-000000000003', ?, 'key', 'Old', 'Old', 0, 0)
            """,
            (fixture_account,),
        )
        connection.execute("PRAGMA user_version = 3")

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT id FROM users").fetchall() == [(retained_account,)]
        assert connection.execute("SELECT count(*) FROM projects").fetchone() == (0,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert "basic_auth_identities" not in tables
