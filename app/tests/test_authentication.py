import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.testclient import TestClient

from running_drafts.api import create_app
from running_drafts.auth import authenticated_user_id
from running_drafts.settings import AppEnvironment, AuthMode, Settings


def run_admin(
    command: str,
    database_path: Path,
    *,
    app_env: str = "test",
    auth_mode: str = "basic",
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": app_env,
        "AUTH_MODE": auth_mode,
        "DATABASE_PATH": str(database_path),
        "BASIC_AUTH_USERNAMES": json.dumps(["Alice"]),
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def make_settings(database_path: Path) -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        auth_mode=AuthMode.BASIC,
        database_path=database_path,
        allowed_origins=["https://editor.test"],
        basic_auth_usernames=["Alice"],
        basic_auth_password="shared-password",
    )


def authenticated_client(database_path: Path) -> TestClient:
    app = create_app(make_settings(database_path))

    @app.get("/api/v1/test-identity")
    def test_identity(
        user_id: Annotated[UUID, Depends(authenticated_user_id)],
    ) -> dict[str, UUID]:
        return {"userId": user_id}

    return TestClient(app)


def test_fixtures_are_idempotent_and_authentication_resolves_the_user(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_admin("migrate", database_path).returncode == 0

    first = run_admin("fixtures", database_path)
    second = run_admin("fixtures", database_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["insertedUsers"] == 1
    assert json.loads(second.stdout)["insertedUsers"] == 0

    with authenticated_client(database_path) as client:
        response = client.get(
            "/api/v1/test-identity",
            auth=("Alice", "shared-password"),
        )

    assert response.status_code == 200
    assert UUID(response.json()["userId"]).version == 4


def test_invalid_basic_credentials_are_indistinguishable(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_admin("migrate", database_path).returncode == 0
    assert run_admin("fixtures", database_path).returncode == 0

    with authenticated_client(database_path) as client:
        responses = [
            client.get("/api/v1/test-identity"),
            client.get(
                "/api/v1/test-identity",
                auth=("unknown", "shared-password"),
            ),
            client.get(
                "/api/v1/test-identity",
                auth=("Alice", "wrong-password"),
            ),
        ]

    assert {response.status_code for response in responses} == {401}
    assert {response.headers["www-authenticate"] for response in responses} == {"Basic"}
    assert len({response.content for response in responses}) == 1
    assert responses[0].json()["code"] == "authentication_required"


def test_missing_fixture_for_valid_credentials_is_an_operator_error(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_admin("migrate", database_path).returncode == 0

    with authenticated_client(database_path) as client:
        response = client.get(
            "/api/v1/test-identity",
            auth=("Alice", "shared-password"),
        )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_configuration_error"


def test_fixtures_reject_production_and_auth0_without_creating_a_database(
    tmp_path: Path,
) -> None:
    production_database = tmp_path / "production.sqlite3"
    auth0_database = tmp_path / "auth0.sqlite3"

    production = run_admin("fixtures", production_database, app_env="production")
    auth0 = run_admin("fixtures", auth0_database, auth_mode="auth0")

    assert production.returncode != 0
    assert auth0.returncode != 0
    assert not production_database.exists()
    assert not auth0_database.exists()
