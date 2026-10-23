import json
import os
import subprocess
from pathlib import Path

APP_DIRECTORY = Path(__file__).parents[1]


def test_compose_publishes_separate_api_and_database_admin_services() -> None:
    environment = os.environ | {
        "APP_ENV": "development",
        "ALLOWED_ORIGINS": json.dumps(["http://localhost:5173"]),
        "PUBLIC_EDITOR_URL": "http://localhost:5173",
    }
    result = subprocess.run(
        ["docker", "compose", "--profile", "tools", "config", "--format", "json"],
        cwd=APP_DIRECTORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    configuration = json.loads(result.stdout)
    assert set(configuration["services"]) == {"api", "db-admin"}
    assert configuration["services"]["api"]["healthcheck"]["test"]
    assert configuration["services"]["db-admin"]["profiles"] == ["tools"]
    assert "application-data" in configuration["volumes"]
    assert configuration["services"]["api"]["build"]["target"] == "api"
    assert configuration["services"]["api"]["environment"]["MAX_STAGED_DOWNLOAD_FILES"] == "20"
    assert configuration["services"]["api"]["environment"][
        "TEST_CONTROL_BOOTSTRAP_CREDENTIAL"
    ] == ""
    assert configuration["services"]["api"]["environment"]["TEST_CONTROL_DERIVATION_KEY"] == ""
    assert "TEST_CONTROL_BOOTSTRAP_CREDENTIAL" not in configuration["services"]["db-admin"][
        "environment"
    ]
    assert configuration["services"]["db-admin"]["build"]["target"] == "db-admin"


def test_deployment_compose_uses_tagged_image_loopback_and_host_storage(
    tmp_path: Path,
) -> None:
    environment = os.environ | {
        "APP_ENV": "production",
        "ALLOWED_ORIGINS": json.dumps(["https://drafts.handsfree.vc"]),
        "BACKUP_DIRECTORY": str(tmp_path / "backups"),
        "DATA_DIRECTORY": str(tmp_path / "data"),
        "HTTP_PORT": "18082",
        "IMAGE_TAG": "0.1.0",
        "PUBLIC_EDITOR_URL": "https://drafts.handsfree.vc",
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            "compose.deployment.yaml",
            "--profile",
            "tools",
            "config",
            "--format",
            "json",
        ],
        cwd=APP_DIRECTORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    configuration = json.loads(result.stdout)
    assert set(configuration["services"]) == {"api", "db-admin"}
    for service in configuration["services"].values():
        assert service["image"] == "running-drafts-backend:0.1.0"
        assert "build" not in service
    assert configuration["services"]["api"]["ports"] == [
        {
            "mode": "ingress",
            "target": 8000,
            "published": "18082",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        },
        {
            "mode": "ingress",
            "target": 8000,
            "published": "18082",
            "protocol": "tcp",
            "host_ip": "::1",
        },
    ]
    assert {volume["source"] for volume in configuration["services"]["api"]["volumes"]} == {
        str(tmp_path / "backups"),
        str(tmp_path / "data"),
    }
    api_environment = configuration["services"]["api"]["environment"]
    assert "AUTH_MODE" not in api_environment
    assert "BASIC_AUTH_USERNAMES" not in api_environment
    assert api_environment["PUBLIC_EDITOR_URL"] == "https://drafts.handsfree.vc"
    assert api_environment["SESSION_COOKIE_SECURE"] == "true"
