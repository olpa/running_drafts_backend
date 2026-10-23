import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from running_drafts.api import create_app
from running_drafts.settings import AppEnvironment, Settings


def run_command(
    command: str,
    database_path: Path,
    **environment_overrides: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": "test",
        "DATABASE_PATH": str(database_path),
        "DATABASE_BUSY_TIMEOUT_MS": "25",
        **environment_overrides,
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_incremental_vacuum_is_an_explicit_bounded_command(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_command("migrate", database_path).returncode == 0

    result = run_command("incremental-vacuum", database_path)

    assert result.returncode == 0, result.stderr
    event = json.loads(result.stdout)
    assert event["event"] == "database_incrementally_vacuumed"
    assert event["pagesBefore"] >= event["pagesAfter"]
    assert event["reclaimedPages"] == event["pagesBefore"] - event["pagesAfter"]


def test_migrate_refuses_a_future_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "future.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 999")

    result = run_command("migrate", database_path)

    assert result.returncode != 0
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (999,)


def test_security_sensitive_settings_are_strict(tmp_path: Path) -> None:
    common = {
        "database_path": tmp_path / "application.sqlite3",
        "public_editor_url": "https://editor.test",
    }

    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnvironment.DEVELOPMENT,
            allowed_origins=["*"],
            **common,
        )
    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            allowed_origins=["https://editor.example"],
            **common | {"public_editor_url": "http://editor.example"},
        )


def test_staged_download_limit_defaults_to_twenty_and_must_be_positive(
    tmp_path: Path,
) -> None:
    common = {
        "app_env": AppEnvironment.TEST,
        "database_path": tmp_path / "application.sqlite3",
        "allowed_origins": ["https://editor.test"],
        "public_editor_url": "https://editor.test",
    }

    assert Settings(**common).max_staged_download_files == 20
    with pytest.raises(ValidationError):
        Settings(**common, max_staged_download_files=0)


def test_oidc_starts_without_a_configured_provider(tmp_path: Path) -> None:
    settings = Settings(
        app_env=AppEnvironment.PRODUCTION,
        database_path=tmp_path / "application.sqlite3",
        allowed_origins=["https://editor.example"],
        public_editor_url="https://editor.example",
    )

    assert create_app(settings).state.provider_registry.configured() == ()


def test_serve_reports_partial_oidc_configuration_as_a_structured_error(
    tmp_path: Path,
) -> None:
    result = run_command(
        "serve",
        tmp_path / "missing.sqlite3",
        APP_ENV="production",
        ALLOWED_ORIGINS=json.dumps(["https://editor.example"]),
        PUBLIC_EDITOR_URL="https://editor.example",
        GOOGLE_CLIENT_ID="configured-without-secret",
    )

    assert result.returncode != 0
    assert json.loads(result.stderr)["event"] == "command_failed"
    assert "incomplete" in json.loads(result.stderr)["error"]
