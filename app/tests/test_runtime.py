import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from running_drafts.api import create_app
from running_drafts.settings import AppEnvironment, AuthMode, Settings


def run_command(
    command: str,
    database_path: Path,
    **environment_overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": "test",
        "AUTH_MODE": "basic",
        "DATABASE_PATH": str(database_path),
        "DATABASE_BUSY_TIMEOUT_MS": "25",
        "BASIC_AUTH_USERNAMES": json.dumps(["Alice"]),
        **environment_overrides,
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_fixtures_never_create_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite3"

    result = run_command("fixtures", database_path)

    assert result.returncode != 0
    assert not database_path.exists()
    assert json.loads(result.stderr)["event"] == "command_failed"


def test_migrate_refuses_a_future_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "future.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 999")

    result = run_command("migrate", database_path)

    assert result.returncode != 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (999,)


def test_fixtures_surface_a_busy_database_without_partial_writes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "busy.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    lock = sqlite3.connect(database_path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        result = run_command("fixtures", database_path)
    finally:
        lock.rollback()
        lock.close()

    assert result.returncode != 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_security_sensitive_settings_are_strict(tmp_path: Path) -> None:
    common = {
        "database_path": tmp_path / "application.sqlite3",
        "basic_auth_usernames": ["Alice"],
        "basic_auth_password": "password",
    }

    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnvironment.DEVELOPMENT,
            auth_mode=AuthMode.BASIC,
            allowed_origins=["*"],
            **common,
        )
    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnvironment.DEVELOPMENT,
            auth_mode=AuthMode.BASIC,
            allowed_origins=["https://editor.test"],
            **common | {"basic_auth_usernames": ["bad user"]},
        )
    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            auth_mode=AuthMode.BASIC,
            allowed_origins=["https://editor.example"],
            **common,
        )


def test_auth0_fails_when_the_application_starts(tmp_path: Path) -> None:
    settings = Settings(
        app_env=AppEnvironment.PRODUCTION,
        auth_mode=AuthMode.AUTH0,
        database_path=tmp_path / "application.sqlite3",
        allowed_origins=["https://editor.example"],
    )

    with (
        pytest.raises(RuntimeError, match="AUTH_MODE=auth0 is not implemented"),
        TestClient(create_app(settings)),
    ):
        pass


def test_serve_reports_unsupported_auth0_as_a_structured_error(
    tmp_path: Path,
) -> None:
    result = run_command(
        "serve",
        tmp_path / "missing.sqlite3",
        APP_ENV="production",
        AUTH_MODE="auth0",
        ALLOWED_ORIGINS=json.dumps(["https://editor.example"]),
    )

    assert result.returncode != 0
    assert json.loads(result.stderr)["event"] == "command_failed"
    assert "not implemented" in json.loads(result.stderr)["error"]
