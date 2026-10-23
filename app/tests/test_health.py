import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from running_drafts.api import create_app
from running_drafts.settings import AppEnvironment, Settings


def make_settings(database_path: Path) -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        database_path=database_path,
        allowed_origins=["https://editor.test"],
        public_editor_url="https://editor.test",
    )


def migrate_database(database_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "running_drafts", "migrate"],
        env=os.environ | {"APP_ENV": "test", "DATABASE_PATH": str(database_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_liveness_does_not_access_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite3"

    with TestClient(create_app(make_settings(database_path))) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not database_path.exists()


def test_production_health_is_public_with_oidc(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    migrate_database(database_path)
    settings = Settings(
        app_env=AppEnvironment.PRODUCTION,
        database_path=database_path,
        allowed_origins=["https://drafts.handsfree.vc"],
        public_editor_url="https://drafts.handsfree.vc",
    )

    with TestClient(create_app(settings)) as client:
        liveness = client.get("/health/live")
        readiness = client.get("/health/ready")

    assert liveness.status_code == 200
    assert readiness.status_code == 200
    assert "www-authenticate" not in liveness.headers
    assert "www-authenticate" not in readiness.headers


def test_readiness_does_not_create_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite3"

    with TestClient(create_app(make_settings(database_path))) as client:
        response = client.get("/health/ready")
        liveness = client.get("/health/live")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Database not ready",
        "status": 503,
        "detail": "The application database is not ready.",
        "code": "database_not_ready",
    }
    assert liveness.status_code == 200
    assert not database_path.exists()


def test_readiness_requires_the_exact_migration_head(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    migrate_database(database_path)

    with TestClient(create_app(make_settings(database_path))) as client:
        assert client.get("/health/ready").json() == {"status": "ok"}

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 0")

    with TestClient(create_app(make_settings(database_path))) as client:
        stale = client.get("/health/ready")

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with TestClient(create_app(make_settings(database_path))) as client:
        future = client.get("/health/ready")

    assert stale.status_code == 503
    assert stale.json()["code"] == "database_not_ready"
    assert future.status_code == 503
    assert future.json()["code"] == "database_not_ready"
