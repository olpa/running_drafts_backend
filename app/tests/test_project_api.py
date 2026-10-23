import asyncio
import errno
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryFile
from threading import Event, Lock
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from running_drafts.api import create_app
from running_drafts.application_sessions import replace_application_session
from running_drafts.oauth import ProviderClaims
from running_drafts.oauth_identities import resolve_or_provision_identity
from running_drafts.project_api import project_storage
from running_drafts.project_storage import ProjectStorage
from running_drafts.session_cookie import SESSION_COOKIE_NAME
from running_drafts.settings import AppEnvironment, Settings

MEDIARECORDER_FIXTURES = Path(__file__).parent / "fixtures" / "mediarecorder"
AUTH_TOKENS: dict[str, str] = {}


def run_admin(command: str, database_path: Path) -> subprocess.CompletedProcess[str]:
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


def settings_for(database_path: Path) -> Settings:
    return Settings(
        app_env=AppEnvironment.TEST,
        database_path=database_path,
        database_busy_timeout_ms=100,
        allowed_origins=["https://editor.test"],
        public_editor_url="https://editor.test",
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "application.sqlite3"
    assert run_admin("migrate", path).returncode == 0
    settings = settings_for(path)
    AUTH_TOKENS.clear()
    for username in ("Alice", "Bob"):
        identity = resolve_or_provision_identity(
            settings,
            ProviderClaims(
                provider="fixture",
                issuer="urn:running-drafts:backend-tests",
                subject=username.lower(),
            ),
        )
        AUTH_TOKENS[username] = replace_application_session(
            settings,
            identity.account_id,
            None,
        ).token
    return path


@pytest.fixture
def client(database_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings_for(database_path))) as test_client:
        yield test_client


def auth(username: str = "Alice") -> dict[str, str]:
    return {
        "Cookie": f"{SESSION_COOKIE_NAME}={AUTH_TOKENS[username]}",
        "Origin": "https://editor.test",
    }


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
        headers=auth(username),
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
        headers=auth(username),
    )
    return response.json(), response


def upload_frame(
    client: TestClient,
    audio_asset_id: str,
    sequence: int,
    *,
    payload: bytes = b"frame-one",
    start_ms: int = 0,
    end_ms: int = 1_000,
    content_type: str = "audio/webm;codecs=opus",
    username: str = "Alice",
):
    return client.put(
        f"/api/v1/audio-assets/{audio_asset_id}/frames/{sequence}",
        content=payload,
        headers=auth(username)
        | {
            "Content-Type": content_type,
            "X-Frame-Start-Ms": str(start_ms),
            "X-Frame-End-Ms": str(end_ms),
        },
    )


def user_id(database_path: Path, username: str) -> UUID:
    with sqlite3.connect(database_path) as connection:
        value = connection.execute(
            "SELECT account_id FROM provider_identities WHERE issuer = ? AND subject = ?",
            ("urn:running-drafts:backend-tests", username.lower()),
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
            headers=auth(),
        )
        replay = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "Café notes"},
            headers=auth(),
        )
        conflict = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "Different"},
            headers=auth(),
        )
        renamed = test_client.patch(
            f"/api/v1/projects/{created.json()['id']}",
            json={"title": "Renamed"},
            headers=auth(),
        )
        unchanged = test_client.patch(
            f"/api/v1/projects/{created.json()['id']}",
            json={"title": " Renamed "},
            headers=auth(),
        )
        replay_after_rename = test_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(key), "title": "Café notes"},
            headers=auth(),
        )
        tied = create_project(test_client, title="Tied")
        listed = test_client.get("/api/v1/projects", headers=auth())

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
        headers=auth(),
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
    detail = client.get(f"/api/v1/projects/{project['id']}", headers=auth())

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


def test_audio_frame_upload_is_acknowledged_and_visible_in_project_detail(
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])

    uploaded = upload_frame(client, section["audioAssetId"], 0)
    detail = client.get(f"/api/v1/projects/{project['id']}", headers=auth())

    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {
        "sequence": 0,
        "acknowledged": True,
        "sha256": "70a09fbff3df33ed9f6db65155e3a6e166de73e2ceab15d42103d688fc904eaa",
        "nextExpectedSequence": 1,
    }
    asset = detail.json()["document"]["sections"][0]["audioAsset"]
    assert asset["frameCount"] == 1
    assert asset["observedDurationMs"] == 1_000
    assert asset["storedBytes"] == 9


def test_audio_frame_upload_enforces_body_media_and_timing_rules(
    database_path: Path,
) -> None:
    settings = settings_for(database_path).model_copy(
        update={"max_transport_frame_bytes": 8}
    )
    with TestClient(create_app(settings)) as test_client:
        project = create_project(test_client)
        section, _response = create_section(test_client, project["id"])
        endpoint = f"/api/v1/audio-assets/{section['audioAssetId']}/frames/0"

        zero = upload_frame(test_client, section["audioAssetId"], 0, payload=b"")
        oversized = test_client.put(
            endpoint,
            content=b"123456789",
            headers=auth()
            | {
                "Content-Type": "audio/webm;codecs=opus",
                "Content-Length": "1",
                "X-Frame-Start-Ms": "0",
                "X-Frame-End-Ms": "1000",
            },
        )
        unsupported = upload_frame(
            test_client,
            section["audioAssetId"],
            0,
            payload=b"frame",
            content_type="audio/ogg;codecs=opus",
        )
        backwards = upload_frame(
            test_client,
            section["audioAssetId"],
            0,
            payload=b"frame",
            start_ms=1_000,
            end_ms=1_000,
        )
        exact_limit = upload_frame(
            test_client,
            section["audioAssetId"],
            0,
            payload=b"12345678",
        )

    assert zero.status_code == 422
    assert zero.json()["code"] == "request_validation_error"
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "frame_too_large"
    assert unsupported.status_code == 415
    assert unsupported.json()["code"] == "unsupported_media_type"
    assert backwards.status_code == 422
    assert backwards.json()["code"] == "request_validation_error"
    assert exact_limit.status_code == 200


def test_audio_frame_http_replays_and_conflicts_follow_asset_state(
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    first = upload_frame(client, asset_id, 0)
    second = upload_frame(
        client,
        asset_id,
        1,
        payload=b"frame-two",
        start_ms=1_000,
        end_ms=2_000,
    )

    replay = upload_frame(
        client,
        asset_id,
        0,
        content_type=' AUDIO/WEBM ; Codecs = "OPUS" ',
    )
    gap = upload_frame(
        client,
        asset_id,
        3,
        payload=b"gap",
        start_ms=2_000,
        end_ms=3_000,
    )
    discontinuity = upload_frame(
        client,
        asset_id,
        2,
        payload=b"discontinuous",
        start_ms=2_001,
        end_ms=3_000,
    )
    changed_replay = upload_frame(client, asset_id, 0, payload=b"changed")
    sealed = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 2},
        headers=auth(),
    )
    replay_after_seal = upload_frame(client, asset_id, 0)
    new_after_seal = upload_frame(
        client,
        asset_id,
        2,
        payload=b"new",
        start_ms=2_000,
        end_ms=3_000,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["nextExpectedSequence"] == 2
    for conflict in (gap, discontinuity, changed_replay, new_after_seal):
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "frame_conflict"
    assert sealed.status_code == 200
    assert replay_after_seal.status_code == 200
    assert replay_after_seal.json()["nextExpectedSequence"] == 2


def test_cross_user_access_is_privacy_preserving(client: TestClient) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])

    responses = [
        client.get(f"/api/v1/projects/{project['id']}", headers=auth("Bob")),
        client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"title": "Stolen"},
            headers=auth("Bob"),
        ),
        client.post(
            f"/api/v1/projects/{project['id']}/sections",
            json={
                "idempotencyKey": str(uuid4()),
                "mediaType": "audio/webm;codecs=opus",
            },
            headers=auth("Bob"),
        ),
        client.post(
            f"/api/v1/audio-assets/{section['audioAssetId']}/interrupt",
            headers=auth("Bob"),
        ),
        upload_frame(
            client,
            section["audioAssetId"],
            0,
            payload=b"private",
            username="Bob",
        ),
    ]
    missing = client.get(f"/api/v1/projects/{uuid4()}", headers=auth("Bob"))

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
        headers=auth(),
    )
    replay = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 0},
        headers=auth(),
    )
    changed_count = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 1},
        headers=auth(),
    )
    opposite = client.post(
        f"/api/v1/audio-assets/{asset_id}/interrupt",
        headers=auth(),
    )

    interrupted_section, _response = create_section(client, project["id"])
    interrupted = client.post(
        f"/api/v1/audio-assets/{interrupted_section['audioAssetId']}/interrupt",
        headers=auth(),
    )
    interrupted_replay = client.post(
        f"/api/v1/audio-assets/{interrupted_section['audioAssetId']}/interrupt",
        headers=auth(),
    )

    assert completed.status_code == 200
    assert completed.json()["isSealed"] is True
    assert completed.json()["sealReason"] == "completed"
    assert completed.json()["frameCount"] == 0
    assert completed.json()["storedBytes"] == 0
    assert replay.json() == completed.json()
    for conflict in (changed_count, opposite):
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "audio_asset_seal_conflict"
    assert interrupted.json()["sealReason"] == "interrupted"
    assert interrupted_replay.json() == interrupted.json()


def test_sealed_audio_download_returns_exact_ordered_frames(client: TestClient) -> None:
    project = create_project(client, title="October Notes")
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    assert upload_frame(client, asset_id, 0, payload=b"frame-one").status_code == 200
    assert (
        upload_frame(
            client,
            asset_id,
            1,
            payload=b"frame-two",
            start_ms=1_000,
            end_ms=2_000,
        ).status_code
        == 200
    )
    sealed = client.post(
        f"/api/v1/audio-assets/{asset_id}/complete",
        json={"frameCount": 2},
        headers=auth(),
    )
    assert sealed.status_code == 200

    downloaded = client.get(
        f"/api/v1/audio-assets/{asset_id}/download",
        headers=auth(),
    )

    assert downloaded.status_code == 200
    assert downloaded.content == b"frame-oneframe-two"
    assert downloaded.headers["content-type"] == "audio/webm;codecs=opus"
    assert downloaded.headers["content-length"] == "18"
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="October-Notes-section-1.webm"; '
        "filename*=UTF-8''October-Notes-section-1.webm"
    )


def test_audio_download_requires_authentication_ownership_and_sealing(
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    endpoint = f"/api/v1/audio-assets/{section['audioAssetId']}/download"

    unauthenticated = client.get(endpoint)
    open_asset = client.get(endpoint, headers=auth())
    unowned = client.get(endpoint, headers=auth("Bob"))
    missing = client.get(f"/api/v1/audio-assets/{uuid4()}/download", headers=auth("Bob"))

    assert unauthenticated.status_code == 401
    assert open_asset.status_code == 409
    assert open_asset.json()["code"] == "asset_not_sealed"
    assert unowned.status_code == 404
    assert unowned.content == missing.content


def test_zero_frame_download_uses_empty_fixture_and_unicode_filename(
    database_path: Path,
    client: TestClient,
) -> None:
    project = create_project(client, title="Žlutý 東京 / Notes 🎙️")
    completed, _response = create_section(client, project["id"])
    sealed = client.post(
        f"/api/v1/audio-assets/{completed['audioAssetId']}/complete",
        json={"frameCount": 0},
        headers=auth(),
    )
    assert sealed.status_code == 200

    def unexpected_temporary_file():
        raise AssertionError("zero-frame downloads must not create a temporary file")

    direct_fixture_storage = ProjectStorage(
        database_path,
        busy_timeout_ms=100,
        temporary_file_factory=unexpected_temporary_file,
    )
    client.app.dependency_overrides[project_storage] = lambda: direct_fixture_storage
    try:
        downloaded = client.get(
            f"/api/v1/audio-assets/{completed['audioAssetId']}/download",
            headers=auth(),
        )
    finally:
        client.app.dependency_overrides.pop(project_storage)

    expected_name = "Žlutý-東京-Notes-section-1.webm"
    expected_bytes = files("running_drafts").joinpath("assets/empty-audio.webm").read_bytes()
    assert downloaded.status_code == 200
    assert downloaded.content == expected_bytes
    assert downloaded.headers["content-length"] == str(len(expected_bytes))
    assert downloaded.headers["content-disposition"] == (
        'attachment; filename="download-section-1.webm"; '
        f"filename*=UTF-8''{quote(expected_name)}"
    )
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert "Content-Disposition" in downloaded.headers["access-control-expose-headers"]


def test_download_filename_preserves_suffix_within_utf8_length_limit(
    client: TestClient,
) -> None:
    project = create_project(client, title="界" * 200)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    assert (
        client.post(
            f"/api/v1/audio-assets/{asset_id}/interrupt",
            headers=auth(),
        ).status_code
        == 200
    )

    downloaded = client.get(
        f"/api/v1/audio-assets/{asset_id}/download",
        headers=auth(),
    )

    encoded_name = downloaded.headers["content-disposition"].split("filename*=UTF-8''", 1)[1]
    filename = unquote(encoded_name)
    assert len(filename.encode()) <= 240
    assert filename.endswith("-section-1-interrupted.webm")
    assert filename.startswith("界")
    assert 'filename="download-section-1-interrupted.webm"' in downloaded.headers[
        "content-disposition"
    ]


def test_inconsistent_audio_download_returns_bounded_assembly_failure(
    database_path: Path,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    assert upload_frame(client, asset_id, 0, payload=b"frame").status_code == 200
    assert (
        client.post(
            f"/api/v1/audio-assets/{asset_id}/complete",
            json={"frameCount": 1},
            headers=auth(),
        ).status_code
        == 200
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE audio_assets SET stored_bytes = 999 WHERE id = ?",
            (asset_id,),
        )

    with caplog.at_level("ERROR"):
        response = client.get(
            f"/api/v1/audio-assets/{asset_id}/download",
            headers=auth(),
        )

    assert response.status_code == 500
    assert response.json()["code"] == "audio_assembly_failed"
    record = next(
        record for record in caplog.records if record.msg.startswith("audio assembly failed")
    )
    message = record.getMessage()
    assert f"audio_asset_id={asset_id}" in message
    assert "expected_bytes=999" in message
    assert "actual_bytes=5" in message
    assert message.count("9dff50df08c635815f4b19da10f756605a34a79a48d4ba48712782502975a70e") == 1


def test_temporary_disk_exhaustion_returns_storage_unavailable(
    database_path: Path,
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    assert upload_frame(client, asset_id, 0, payload=b"frame").status_code == 200
    assert (
        client.post(
            f"/api/v1/audio-assets/{asset_id}/complete",
            json={"frameCount": 1},
            headers=auth(),
        ).status_code
        == 200
    )

    def exhausted_temporary_file():
        raise OSError(errno.ENOSPC, "injected full temporary disk")

    exhausted_storage = ProjectStorage(
        database_path,
        busy_timeout_ms=100,
        temporary_file_factory=exhausted_temporary_file,
    )
    client.app.dependency_overrides[project_storage] = lambda: exhausted_storage
    try:
        response = client.get(
            f"/api/v1/audio-assets/{asset_id}/download",
            headers=auth(),
        )
    finally:
        client.app.dependency_overrides.pop(project_storage)

    assert response.status_code == 503
    assert response.json()["code"] == "storage_unavailable"


def test_download_capacity_rejects_before_staging_and_returns_after_streaming(
    database_path: Path,
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    assert upload_frame(client, asset_id, 0, payload=b"frame").status_code == 200
    assert (
        client.post(
            f"/api/v1/audio-assets/{asset_id}/complete",
            json={"frameCount": 1},
            headers=auth(),
        ).status_code
        == 200
    )

    all_streams_started = Event()
    allow_streaming = Event()
    factory_lock = Lock()
    files_created = 0
    streams_started = 0

    class BlockingFile:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
            self.started = False

        def read(self, size: int = -1):
            nonlocal streams_started
            with factory_lock:
                if not self.started:
                    self.started = True
                    streams_started += 1
                    if streams_started == 20:
                        all_streams_started.set()
            assert allow_streaming.wait(timeout=10)
            return self.wrapped.read(size)

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

    def temporary_file_factory():
        nonlocal files_created
        with factory_lock:
            files_created += 1
            should_block = files_created <= 20
        temporary = TemporaryFile(mode="w+b")  # noqa: SIM115 - response owns the file
        return BlockingFile(temporary) if should_block else temporary

    settings = settings_for(database_path).model_copy(
        update={"max_staged_download_files": 20}
    )
    app = create_app(settings)
    app.dependency_overrides[project_storage] = lambda: ProjectStorage(
        database_path,
        busy_timeout_ms=100,
        temporary_file_factory=temporary_file_factory,
    )
    endpoint = f"/api/v1/audio-assets/{asset_id}/download"
    with TestClient(app) as first_client, TestClient(app) as second_client:
        with ThreadPoolExecutor(max_workers=20) as executor:
            first_twenty = [
                executor.submit(first_client.get, endpoint, headers=auth())
                for _index in range(20)
            ]
            assert all_streams_started.wait(timeout=10)

            unauthenticated = second_client.get(endpoint)
            assert unauthenticated.status_code == 401
            rejected = second_client.get(
                f"/api/v1/audio-assets/{uuid4()}/download",
                headers=auth(),
            )
            assert rejected.status_code == 429
            assert rejected.json()["code"] == "concurrent_download_limit_exceeded"
            assert "retry-after" not in rejected.headers
            assert files_created == 20

            allow_streaming.set()
            assert {request.result(timeout=10).status_code for request in first_twenty} == {
                200
            }

        accepted_after_cleanup = second_client.get(endpoint, headers=auth())

    assert accepted_after_cleanup.status_code == 200
    assert files_created == 21


def test_client_disconnect_closes_staged_file_and_releases_capacity(
    database_path: Path,
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    payload = b"x" * (128 * 1024)
    assert upload_frame(client, asset_id, 0, payload=payload).status_code == 200
    assert (
        client.post(
            f"/api/v1/audio-assets/{asset_id}/complete",
            json={"frameCount": 1},
            headers=auth(),
        ).status_code
        == 200
    )

    staged_file_closed = Event()
    factory_lock = Lock()
    files_created = 0

    class ObservedFile:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped

        def close(self) -> None:
            self.wrapped.close()
            staged_file_closed.set()

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

    def temporary_file_factory():
        nonlocal files_created
        with factory_lock:
            files_created += 1
            observe = files_created == 1
        temporary = TemporaryFile(mode="w+b")  # noqa: SIM115 - response owns the file
        return ObservedFile(temporary) if observe else temporary

    app = create_app(
        settings_for(database_path).model_copy(update={"max_staged_download_files": 1})
    )
    app.dependency_overrides[project_storage] = lambda: ProjectStorage(
        database_path,
        busy_timeout_ms=100,
        temporary_file_factory=temporary_file_factory,
    )
    endpoint = f"/api/v1/audio-assets/{asset_id}/download"
    session_cookie = f"{SESSION_COOKIE_NAME}={AUTH_TOKENS['Alice']}".encode()

    async def disconnect_during_stream() -> None:
        request_sent = False
        first_body = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await first_body.wait()
            return {"type": "http.disconnect"}

        async def send(message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                first_body.set()
                await asyncio.Event().wait()

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": endpoint,
                "raw_path": endpoint.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"cookie", session_cookie)],
                "client": ("testclient", 123),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )

    asyncio.run(disconnect_during_stream())

    assert staged_file_closed.wait(timeout=1)
    with TestClient(app) as retry_client:
        retry = retry_client.get(endpoint, headers=auth())
    assert retry.status_code == 200


def test_disconnect_before_first_body_closes_file_and_releases_capacity(
    database_path: Path,
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    assert upload_frame(client, asset_id, 0, payload=b"frame").status_code == 200
    assert (
        client.post(
            f"/api/v1/audio-assets/{asset_id}/complete",
            json={"frameCount": 1},
            headers=auth(),
        ).status_code
        == 200
    )

    staged_file_closed = Event()
    staged_file_read = Event()

    class ObservedFile:
        def __init__(self) -> None:
            self.wrapped = TemporaryFile(mode="w+b")  # noqa: SIM115 - response owns it

        def read(self, size: int = -1):
            staged_file_read.set()
            return self.wrapped.read(size)

        def close(self) -> None:
            self.wrapped.close()
            staged_file_closed.set()

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

    files_created = 0

    def temporary_file_factory():
        nonlocal files_created
        files_created += 1
        return ObservedFile() if files_created == 1 else TemporaryFile(mode="w+b")

    app = create_app(
        settings_for(database_path).model_copy(update={"max_staged_download_files": 1})
    )
    app.dependency_overrides[project_storage] = lambda: ProjectStorage(
        database_path,
        busy_timeout_ms=100,
        temporary_file_factory=temporary_file_factory,
    )
    endpoint = f"/api/v1/audio-assets/{asset_id}/download"
    session_cookie = f"{SESSION_COOKIE_NAME}={AUTH_TOKENS['Alice']}".encode()

    async def disconnect_before_body() -> None:
        request_sent = False
        disconnect_returned = asyncio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            disconnect_returned.set()
            return {"type": "http.disconnect"}

        async def send(message) -> None:
            if message["type"] == "http.response.start":
                await disconnect_returned.wait()

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": endpoint,
                "raw_path": endpoint.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"cookie", session_cookie)],
                "client": ("testclient", 123),
                "server": ("testserver", 80),
                "state": {},
            },
            receive,
            send,
        )

    asyncio.run(disconnect_before_body())

    assert not staged_file_read.is_set()
    assert staged_file_closed.wait(timeout=1)
    with TestClient(app) as retry_client:
        retry = retry_client.get(endpoint, headers=auth())
    assert retry.status_code == 200


@pytest.mark.parametrize(
    "case",
    [
        "chromium-completed",
        "chromium-interrupted",
        "firefox-completed",
        "firefox-interrupted",
    ],
)
def test_real_browser_audio_download_is_the_exact_joined_stream(
    client: TestClient,
    case: str,
) -> None:
    fixture = MEDIARECORDER_FIXTURES / case
    metadata = json.loads((fixture / "metadata.json").read_text())
    project = create_project(client, title="Browser Evidence")
    section, _response = create_section(client, project["id"])
    asset_id = section["audioAssetId"]
    start_ms = 0
    for frame in metadata["frames"]:
        end_ms = start_ms + 1_000
        uploaded = upload_frame(
            client,
            asset_id,
            frame["sequence"],
            payload=(fixture / frame["file"]).read_bytes(),
            start_ms=start_ms,
            end_ms=end_ms,
        )
        assert uploaded.status_code == 200, uploaded.text
        start_ms = end_ms
    if metadata["sealReason"] == "completed":
        sealed = client.post(
            f"/api/v1/audio-assets/{asset_id}/complete",
            json={"frameCount": len(metadata["frames"])},
            headers=auth(),
        )
    else:
        sealed = client.post(
            f"/api/v1/audio-assets/{asset_id}/interrupt",
            headers=auth(),
        )
    assert sealed.status_code == 200

    downloaded = client.get(
        f"/api/v1/audio-assets/{asset_id}/download",
        headers=auth(),
    )

    assert downloaded.status_code == 200
    assert downloaded.content == (fixture / "joined.webm").read_bytes()
    expected_suffix = "-interrupted.webm" if case.endswith("interrupted") else ".webm"
    assert f"section-1{expected_suffix}" in downloaded.headers["content-disposition"]


def test_completion_frame_count_requires_a_nonnegative_integer(client: TestClient) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    endpoint = f"/api/v1/audio-assets/{section['audioAssetId']}/complete"

    responses = [
        client.post(endpoint, json={"frameCount": True}, headers=auth()),
        client.post(endpoint, json={"frameCount": -1}, headers=auth()),
        client.post(endpoint, json={"frameCount": 0.0}, headers=auth()),
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
        "/api/v1/audio-assets/{audio_asset_id}/frames/{sequence}",
        "/api/v1/audio-assets/{audio_asset_id}/complete",
        "/api/v1/audio-assets/{audio_asset_id}/interrupt",
        "/api/v1/audio-assets/{audio_asset_id}/download",
    } <= paths.keys()
    create_operation = paths["/api/v1/projects"]["post"]
    assert create_operation["security"] == [{"ApplicationSession": []}]
    assert "Location" in create_operation["responses"]["201"]["headers"]
    assert "WWW-Authenticate" in create_operation["responses"]["401"]["headers"]
    assert "application/problem+json" in create_operation["responses"]["422"]["content"]
    assert "requestBody" not in paths[
        "/api/v1/audio-assets/{audio_asset_id}/interrupt"
    ]["post"]
    frame_operation = paths[
        "/api/v1/audio-assets/{audio_asset_id}/frames/{sequence}"
    ]["put"]
    assert frame_operation["security"] == [{"ApplicationSession": []}]
    assert {
        parameter["name"]: (parameter["in"], parameter["required"])
        for parameter in frame_operation["parameters"]
    } == {
        "audio_asset_id": ("path", True),
        "sequence": ("path", True),
        "X-Frame-Start-Ms": ("header", True),
        "X-Frame-End-Ms": ("header", True),
    }
    frame_schema = frame_operation["requestBody"]["content"][
        "audio/webm;codecs=opus"
    ]["schema"]
    assert frame_schema == {
        "type": "string",
        "format": "binary",
        "minLength": 1,
        "maxLength": 2 * 1024 * 1024,
    }
    assert {"200", "401", "404", "409", "413", "415", "422", "503"} <= set(
        frame_operation["responses"]
    )
    for status_code in ("401", "404", "409", "413", "415", "422", "503"):
        assert (
            "application/problem+json"
            in frame_operation["responses"][status_code]["content"]
        )
    download_operation = paths[
        "/api/v1/audio-assets/{audio_asset_id}/download"
    ]["get"]
    assert download_operation["responses"]["200"]["content"] == {
        "audio/webm;codecs=opus": {
            "schema": {"type": "string", "format": "binary"},
        }
    }
    assert {"Content-Disposition", "Content-Length"} <= set(
        download_operation["responses"]["200"]["headers"]
    )
    assert {"401", "404", "409", "422", "429", "500", "503"} <= set(
        download_operation["responses"]
    )
    for status_code in ("401", "404", "409", "422", "429", "500", "503"):
        assert (
            "application/problem+json"
            in download_operation["responses"][status_code]["content"]
        )


def test_busy_mutation_returns_retryable_problem_details(
    database_path: Path,
    client: TestClient,
) -> None:
    project = create_project(client)
    section, _response = create_section(client, project["id"])
    lock = sqlite3.connect(database_path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        responses = [
            client.post(
                "/api/v1/projects",
                json={"idempotencyKey": str(uuid4()), "title": "Busy"},
                headers=auth(),
            ),
            upload_frame(client, section["audioAssetId"], 0),
        ]
    finally:
        lock.rollback()
        lock.close()

    for response in responses:
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "database_busy"
        assert "retry-after" not in response.headers


def test_failed_frame_transaction_is_not_acknowledged_and_can_be_retried(
    database_path: Path,
) -> None:
    app = create_app(settings_for(database_path))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        project = create_project(test_client)
        section, _response = create_section(test_client, project["id"])
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_test_frame
                BEFORE INSERT ON transport_frames
                BEGIN
                    SELECT RAISE(ABORT, 'injected frame failure');
                END
                """
            )

        failed = upload_frame(test_client, section["audioAssetId"], 0)

        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP TRIGGER reject_test_frame")
        retry = upload_frame(test_client, section["audioAssetId"], 0)
        replay = upload_frame(test_client, section["audioAssetId"], 0)
        detail = test_client.get(f"/api/v1/projects/{project['id']}", headers=auth())

    assert failed.status_code == 500
    assert retry.status_code == 200
    assert replay.status_code == 200
    asset = detail.json()["document"]["sections"][0]["audioAsset"]
    assert asset["frameCount"] == 1
    assert asset["storedBytes"] == 9
