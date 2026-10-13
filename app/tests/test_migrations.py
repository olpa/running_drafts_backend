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
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
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
    } <= tables
    assert "documents" not in tables
    assert all("owner_id" not in columns for columns in child_columns.values())
