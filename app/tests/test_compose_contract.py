import json
import os
import subprocess
from pathlib import Path

APP_DIRECTORY = Path(__file__).parents[1]


def test_compose_publishes_separate_api_and_database_admin_services() -> None:
    environment = os.environ | {
        "APP_ENV": "development",
        "AUTH_MODE": "basic",
        "ALLOWED_ORIGINS": json.dumps(["http://localhost:5173"]),
        "BASIC_AUTH_USERNAMES": json.dumps(["developer"]),
        "BASIC_AUTH_PASSWORD": "development-only",
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
    assert configuration["services"]["db-admin"]["build"]["target"] == "db-admin"
