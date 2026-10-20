import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest

APP_DIRECTORY = Path(__file__).parents[1]
RUN_CONTAINER_TESTS = os.environ.get("RUN_CONTAINER_TESTS") == "1"


def request_json(url: str, *, expected_status: int = 200) -> dict[str, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            assert response.status == expected_status
            return json.load(response)
    except urllib.error.HTTPError as error:
        assert error.status == expected_status
        return json.load(error)


def last_json_event(output: str) -> dict[str, object]:
    for line in reversed(output.splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no structured event in command output: {output}")


@pytest.mark.container
@pytest.mark.skipif(not RUN_CONTAINER_TESTS, reason="set RUN_CONTAINER_TESTS=1")
def test_compose_migrates_seeds_serves_and_restarts() -> None:
    project = f"running_drafts_smoke_{uuid4().hex[:12]}"
    environment = os.environ | {
        "COMPOSE_PROJECT_NAME": project,
        "API_PORT": "0",
        "APP_ENV": "development",
        "AUTH_MODE": "basic",
        "ALLOWED_ORIGINS": json.dumps(["http://localhost:5173"]),
        "BASIC_AUTH_USERNAMES": json.dumps(["developer"]),
        "BASIC_AUTH_PASSWORD": "container-smoke-only",
    }

    def compose(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", *arguments],
            cwd=APP_DIRECTORY,
            env=environment,
            text=True,
            capture_output=True,
            check=check,
        )

    def wait_for(url: str, expected_status: int = 200) -> dict[str, str]:
        deadline = time.monotonic() + 30
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return request_json(url, expected_status=expected_status)
            except (AssertionError, OSError, ValueError) as error:
                last_error = error
                time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {url}") from last_error

    def api_base_url() -> str:
        published = compose("port", "api", "8000").stdout.strip()
        port = int(published.rsplit(":", 1)[1])
        return f"http://127.0.0.1:{port}"

    try:
        migrated = compose("run", "--rm", "--build", "db-admin", "migrate")
        fixtures = compose("run", "--rm", "db-admin", "fixtures")
        compose("up", "-d", "--build", "api")
        identity = compose("exec", "-T", "api", "id", "-u")
        base_url = api_base_url()

        assert last_json_event(migrated.stdout)["schema_version"] == 3
        assert last_json_event(fixtures.stdout)["insertedUsers"] == 1
        assert identity.stdout.strip() == "1000"
        assert wait_for(f"{base_url}/health/live") == {"status": "ok"}
        assert wait_for(f"{base_url}/health/ready") == {"status": "ok"}

        compose("restart", "api")
        base_url = api_base_url()
        assert wait_for(f"{base_url}/health/ready") == {"status": "ok"}
    finally:
        compose("down", "-v", "--remove-orphans", check=False)


@pytest.mark.container
@pytest.mark.skipif(not RUN_CONTAINER_TESTS, reason="set RUN_CONTAINER_TESTS=1")
def test_make_image_builds_the_api_runtime() -> None:
    image_tag = f"test-{uuid4().hex[:12]}"
    image = f"running-drafts-backend:{image_tag}"
    try:
        build = subprocess.run(
            ["make", f"IMAGE_TAG={image_tag}", "image"],
            cwd=APP_DIRECTORY,
            text=True,
            capture_output=True,
            check=False,
        )
        assert build.returncode == 0, build.stderr

        configuration = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{json .Config.Cmd}} {{json .Config.User}}",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert configuration.returncode == 0, configuration.stderr
        assert configuration.stdout.strip() == '["serve"] "running-drafts"'
    finally:
        subprocess.run(
            ["docker", "image", "rm", image],
            text=True,
            capture_output=True,
            check=False,
        )
