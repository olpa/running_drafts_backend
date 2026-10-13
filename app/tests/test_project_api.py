import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from running_drafts.api import create_app
from running_drafts.project_api import project_storage
from running_drafts.project_storage import ProjectStorage
from running_drafts.settings import AppEnvironment, AuthMode, Settings

PASSWORD = "shared-password"


def run_admin(command: str, database_path: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": "test",
        "AUTH_MODE": "basic",
        "DATABASE_PATH": str(database_path),
        "BASIC_AUTH_USERNAMES": json.dumps(["Alice", "Bob"]),
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def settings_for(database_path: Path) -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        auth_mode=AuthMode.BASIC,
        database_path=database_path,
        database_busy_timeout_ms=100,
        allowed_origins=["https://editor.test"],
        basic_auth_usernames=["Alice", "Bob"],
        basic_auth_password=PASSWORD,
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "application.sqlite3"
    assert run_admin("migrate", path).returncode == 0
    assert run_admin("fixtures", path).returncode == 0
    return path


@pytest.fixture
def client(database_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings_for(database_path))) as test_client:
        yield test_client


def auth(username: str = "Alice") -> tuple[str, str]:
    return username, PASSWORD


def create_project(
    client: TestClient,
    *,
    title: str = "First project",
    key: UUID | None = None,
    username: str = "Alice",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/projects",
        json={"idempotencyKey": str(key or uuid4()), "title": title},
        auth=auth(username),
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_section(
    client: TestClient,
    project_id: str,
    *,
    key: UUID | None = None,
    media_type: str = "audio/webm;codecs=opus",
    username: str = "Alice",
) -> tuple[dict[str, object], object]:
    response = client.post(
        f"/api/v1/projects/{project_id}/sections",
        json={"idempotencyKey": str(key or uuid4()), "mediaType": media_type},
        auth=auth(username),
    )
    return response.json(), response


def user_id(database_path: Path, username: str) -> UUID:
    with sqlite3.connect(database_path) as connection:
        value = connection.execute(
            "SELECT user_id FROM basic_auth_identities WHERE username = ?",
            (username,),
        ).fetchone()[0]
    return UUID(value)


def test_project_normalization_idempotency_rename_and_stable_list_order(
    database_path: Path,
) -> None:
    moments = iter([1_000, 2_000, 2_000])
    storage = ProjectStorage(
        database_path,
        busy_timeout_ms=100,
        clock_ms=lambda: next(moments),
    )
    app = create_app(settings_for(database_path))
    app.dependency_overrides[project_storage] = lambda: storage
    key = uuid4()

    with TestClient(app) as test_client:
        created = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "  Cafe\u0301 notes  "},
            auth=auth(),
        )
        replay = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "Café notes"},
            auth=auth(),
        )
        conflict = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "Different"},
            auth=auth(),
        )
        renamed = test_client.patch(
            f"/api/v1/projects/{created.json()['id']}",
            json={"title": "Renamed"},
            auth=auth(),
        )
        unchanged = test_client.patch(
            f"/api/v1/projects/{created.json()['id']}",
            json={"title": " Renamed "},
            auth=auth(),
        )
        replay_after_rename = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "Café notes"},
            auth=auth(),
        )
        tied = create_project(test_client, title="Tied")
        listed = test_client.get("/api/v1/projects", auth=auth())

    assert created.headers["location"] == f"/api/v1/projects/{created.json()['id']}"
    assert created.json()["title"] == "Café notes"
    assert replay.json() == created.json()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_reused"
    assert renamed.json()["title"] == "Renamed"
    assert renamed.json()["updatedAt"] != created.json()["updatedAt"]
    assert unchanged.json() == renamed.json()
    assert replay_after_rename.json() == renamed.json()
    expected_tied_ids = sorted([created.json()["id"], tied["id"]])
    assert [item["id"] for item in listed.json()["projects"]] == expected_tied_ids


@pytest.mark.parametrize(
    "title",
    ["   ", "inside\u0000control", "x" * 201],
)
def test_invalid_titles_use_problem_details(client: TestClient, title: str) -> None:
    response = client.post(
        "/api/v1/projects",
        json={"idempotencyKey": str(uuid4()), "title": title},
        auth=auth(),
    )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "request_validation_error"


def test_section_creation_is_canonical_idempotent_and_visible_in_document(
    client: TestClient,
) -> None:
    project = create_project(client)
    key = uuid4()

    first_body, first = create_section(
        client,
        project["id"],
        key=key,
        media_type=' AUDIO/WEBM ; Codecs = "OPUS" ',
    )
    replay_body, replay = create_section(
        client,
        project["id"],
        key=key,
    )
    unsupported_body, unsupported = create_section(
        client,
        project["id"],
        media_type="audio/webm",
    )
    detail = client.get(f"/api/v1/projects/{project['id']}", auth=auth())

    assert first.status_code == 201
    assert first.headers["location"] == f"/api/v1/projects/{project['id']}"
    assert replay.status_code == 201
    assert replay_body == first_body
    assert unsupported.status_code == 422, unsupported_body
    section = detail.json()["document"]["sections"][0]
    assert section == {
        "id": first_body["sectionId"],
        "position": 0,
        "audioAsset": {
            "id": first_body["audioAssetId"],
            "mediaType": "audio/webm;codecs=opus",
            "isSealed": False,
            "sealReason": None,
            "sealedAt": None,
            "frameCount": 0,
            "observedDurationMs": 0,
            "storedBytes": 0,
        },
    }


def test_cross_user_access_is_privacy_preserving(client: TestClient) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])

    responses = [
        client.get(f"/api/v1/projects/{project['id']}", auth=auth("Bob")),
        client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"title": "Stolen"},
            auth=auth("Bob"),
        ),
        client.post(
            f"/api/v1/projects/{project['id']}/sections",
            json={
                "idempotencyKey": str(uuid4()),
                "mediaType": "audio/webm;codecs=opus",
            },
            auth=auth("Bob"),
        ),
        client.post(
            f"/api/v1/audio-assets/{section['audioAssetId']}/interrupt",
            auth=auth("Bob"),
        ),
    ]
    missing = client.get(f"/api/v1/projects/{uuid4()}", auth=auth("Bob"))

    assert {response.status_code for response in responses} == {404}
    assert {response.json()["code"] for response in responses} == {"resource_not_found"}
    assert {response.content for response in [*responses, missing]} == {missing.content}


def test_zero_frame_sealing_is_idempotent_and_conflicts_are_stable(
    client: TestClient,
) -> None:
    project = create_project(client)
    completed_section, _response = create_section(client, project["id"])
    asset_id = completed_section["audioAssetId"]

    completed = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 0},
        auth=auth(),
    )
    replay = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 0},
        auth=auth(),
    )
    changed_count = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 1},
        auth=auth(),
    )
    opposite = client.post(
        f"/api/v1/audio-assets/{asset_id}/interrupt",
        auth=auth(),
    )

    interrupted_section, _response = create_section(client, project["id"])
    interrupted = client.post(
        f"/api/v1/audio-assets/{interrupted_section['audioAssetId']}/interrupt",
        auth=auth(),
    )
    interrupted_replay = client.post(
        f"/api/v1/audio-assets/{interrupted_section['audioAssetId']}/interrupt",
        auth=auth(),
    )

    assert completed.status_code == 200
    assert completed.json()["isSealed"] is True
    assert completed.json()["sealReason"] == "completed"
    assert completed.json()["frameCount"] == 0
    assert replay.json() == completed.json()
    for conflict in (changed_count, opposite):
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "audio_asset_seal_conflict"
    assert interrupted.json()["sealReason"] == "interrupted"
    assert interrupted_replay.json() == interrupted.json()


def test_completion_frame_count_requires_a_nonnegative_integer(client: TestClient) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    endpoint = f"/api/v1/audio-assets/{section['audioAssetId']}/complete"

    responses = [
        client.post(endpoint, json={"frameCount": True}, auth=auth()),
        client.post(endpoint, json={"frameCount": -1}, auth=auth()),
        client.post(endpoint, json={"frameCount": 0.0}, auth=auth()),
    ]

    assert {response.status_code for response in responses} == {422}
    assert {response.json()["code"] for response in responses} == {
        "request_validation_error"
    }


def test_openapi_declares_routes_security_locations_and_problem_details(
    database_path: Path,
) -> None:
    schema = create_app(settings_for(database_path)).openapi()
    paths = schema["paths"]

    assert {
        "/api/v1/projects",
        "/api/v1/projects/{project_id}",
        "/api/v1/projects/{project_id}/sections",
        "/api/v1/audio-assets/{audio_asset_id}/complete",
        "/api/v1/audio-assets/{audio_asset_id}/interrupt",
    } <= paths.keys()
    create_operation = paths["/api/v1/projects"]["post"]
    assert create_operation["security"] == [{"HTTPBasic": []}]
    assert "Location" in create_operation["responses"]["201"]["headers"]
    assert "WWW-Authenticate" in create_operation["responses"]["401"]["headers"]
    assert "application/problem+json" in create_operation["responses"]["422"]["content"]
    assert "requestBody" not in paths[
        "/api/v1/audio-assets/{audio_asset_id}/interrupt"
    ]["post"]


def test_busy_mutation_returns_retryable_problem_details(
    database_path: Path,
    client: TestClient,
) -> None:
    lock = sqlite3.connect(database_path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        response = client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(uuid4()), "title": "Busy"},
            auth=auth(),
        )
    finally:
        lock.rollback()
        lock.close()

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "database_busy"
    assert "retry-after" not in response.headers
