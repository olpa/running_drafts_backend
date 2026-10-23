import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from running_drafts.api import create_app
from running_drafts.application_sessions import (
    replace_application_session,
    resolve_application_session,
)
from running_drafts.oauth import ProviderClaims
from running_drafts.oauth_identities import resolve_or_provision_identity
from running_drafts.session_cookie import SESSION_COOKIE_NAME
from running_drafts.settings import AppEnvironment, Settings
from running_drafts.structured_logging import JsonFormatter
from running_drafts.test_control import (
    CONTROL_HEADER,
    DERIVATION_CONTEXT,
    FIXTURE_ACCOUNT_ID,
    create_fixture,
    derive_control_token,
    reset_fixture,
)

BOOTSTRAP = "bootstrap-credential"
DERIVATION_KEY = "derivation-key"
ORIGIN = "https://editor.test"


def migrate_database(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "running_drafts", "migrate"],
        env=os.environ | {"APP_ENV": "test", "DATABASE_PATH": str(path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def settings_for(
    path: Path,
    *,
    bootstrap: str | None = BOOTSTRAP,
    derivation_key: str | None = DERIVATION_KEY,
    environment: AppEnvironment = AppEnvironment.TEST,
    busy_timeout_ms: int = 5_000,
) -> Settings:
    origin = "https://editor.example" if environment is AppEnvironment.PRODUCTION else ORIGIN
    return Settings(
        app_env=environment,
        database_path=path,
        database_busy_timeout_ms=busy_timeout_ms,
        allowed_origins=[origin],
        public_editor_url=origin,
        session_cookie_secure=environment is AppEnvironment.PRODUCTION,
        test_control_bootstrap_credential=bootstrap,
        test_control_derivation_key=derivation_key,
    )


def control_headers(client: TestClient) -> dict[str, str]:
    response = client.get(
        "/api/v1/test-control/challenge",
        headers={"Authorization": f"Bearer {BOOTSTRAP}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["derivation"] == "hmac-sha256"
    assert body["context"] == DERIVATION_CONTEXT
    challenge = base64.urlsafe_b64decode(body["challenge"] + "==")
    return {CONTROL_HEADER: derive_control_token(DERIVATION_KEY, challenge)}


def test_controls_are_absent_unless_both_settings_are_present(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="running_drafts.api")
    configurations = [(None, None), (BOOTSTRAP, None), (None, DERIVATION_KEY)]

    for index, (bootstrap, derivation_key) in enumerate(configurations):
        app = create_app(
            settings_for(
                tmp_path / f"missing-{index}.sqlite3",
                bootstrap=bootstrap,
                derivation_key=derivation_key,
            )
        )
        with TestClient(app) as client:
            assert client.get("/api/v1/test-control/challenge").status_code == 404
            assert client.post("/api/v1/test-control/reset").status_code == 404

    messages = [record.getMessage() for record in caplog.records]
    assert all("test control disabled" in message for message in messages)
    assert BOOTSTRAP not in "\n".join(messages)
    assert DERIVATION_KEY not in "\n".join(messages)


def test_challenge_authentication_stability_and_wire_contract(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path / "missing.sqlite3"))

    with TestClient(app) as client:
        missing = client.get("/api/v1/test-control/challenge")
        wrong = client.get(
            "/api/v1/test-control/challenge",
            headers={"Authorization": "Bearer wrong"},
        )
        first = client.get(
            "/api/v1/test-control/challenge",
            headers={"Authorization": f"Bearer {BOOTSTRAP}"},
        )
        second = client.get(
            "/api/v1/test-control/challenge",
            headers={"Authorization": f"Bearer {BOOTSTRAP}"},
        )

    assert {missing.status_code, wrong.status_code} == {401}
    assert missing.content == wrong.content
    assert missing.headers["www-authenticate"] == "Bearer"
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == second.json()
    assert first.json()["derivation"] == "hmac-sha256"
    assert first.json()["context"] == DERIVATION_CONTEXT
    encoded = first.json()["challenge"]
    assert "=" not in encoded
    assert len(base64.urlsafe_b64decode(encoded + "==")) == 32


def test_derivation_has_a_fixed_cross_implementation_vector() -> None:
    challenge = bytes(range(32))
    expected = base64.urlsafe_b64encode(
        hmac.new(
            b"derivation-key",
            b"running-drafts:test-control:v1:" + challenge,
            hashlib.sha256,
        ).digest()
    ).rstrip(b"=").decode("ascii")

    assert derive_control_token("derivation-key", challenge) == expected
    assert expected == "SDao0o__gWwq2nyNO1DaaNYWr06sR3C3KwHnMPk94nE"


def test_operational_credentials_are_scoped_and_indistinguishable(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    with TestClient(create_app(settings_for(database))) as client:
        valid = control_headers(client)
        responses = [
            client.post("/api/v1/test-control/reset"),
            client.post("/api/v1/test-control/reset", headers={CONTROL_HEADER: "wrong"}),
            client.post(
                "/api/v1/test-control/reset",
                headers={CONTROL_HEADER: BOOTSTRAP},
            ),
        ]
        derived_on_challenge = client.get(
            "/api/v1/test-control/challenge",
            headers={"Authorization": f"Bearer {valid[CONTROL_HEADER]}"},
        )

    assert {response.status_code for response in responses} == {401}
    assert len({response.content for response in responses}) == 1
    assert {response.headers["www-authenticate"] for response in responses} == {
        "TestControl"
    }
    assert derived_on_challenge.status_code == 401


def test_create_issues_an_ordinary_session_and_canonical_profile(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    settings = settings_for(database)

    with TestClient(create_app(settings)) as client:
        headers = control_headers(client)
        created = client.post("/api/v1/test-control/create", headers=headers)
        token = client.cookies[SESSION_COOKIE_NAME]
        me = client.get("/api/v1/me")

    assert created.status_code == 200
    assert created.json()["status"] == "created"
    assert created.json()["accountId"] == str(FIXTURE_ACCOUNT_ID)
    assert created.json()["sessionExpiresAt"] == me.json()["sessionExpiresAt"]
    assert me.json()["account"] == {"id": str(FIXTURE_ACCOUNT_ID)}
    assert me.json()["provider"] == {"id": "fixture", "displayName": "Fixture"}
    assert me.json()["profile"] == {
        "email": "fixture@example.invalid",
        "emailVerified": True,
        "displayName": "Fixture User",
        "givenName": "Fixture",
        "familyName": "User",
        "pictureUrl": None,
        "locale": "en",
    }
    assert token not in created.text
    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT token_digest FROM application_sessions").fetchone()[0]
    assert stored == hashlib.sha256(token.encode()).digest()


def test_repeated_create_preserves_data_and_adds_sessions(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    settings = settings_for(database)
    with TestClient(create_app(settings)) as client:
        headers = control_headers(client)
        assert client.post("/api/v1/test-control/create", headers=headers).status_code == 200
        first_token = client.cookies[SESSION_COOKIE_NAME]
        project = client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(uuid4()), "title": "Preserved"},
            headers={"Origin": ORIGIN},
        )
        assert project.status_code == 201
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                UPDATE provider_identities
                SET email = 'drift@example.test', display_name = 'Drifted'
                WHERE account_id = ?
                """,
                (str(FIXTURE_ACCOUNT_ID),),
            )
        second = client.post("/api/v1/test-control/create", headers=headers)
        second_token = client.cookies[SESSION_COOKIE_NAME]
        listed = client.get("/api/v1/projects")
        me = client.get("/api/v1/me")

    assert second.status_code == 200
    assert first_token != second_token
    assert resolve_application_session(settings, first_token) is not None
    assert resolve_application_session(settings, second_token) is not None
    assert [item["title"] for item in listed.json()["projects"]] == ["Preserved"]
    assert me.json()["profile"]["email"] == "fixture@example.invalid"
    assert me.json()["profile"]["displayName"] == "Fixture User"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM application_sessions").fetchone() == (2,)


def test_reset_cascades_fixture_state_and_preserves_unrelated_state(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    settings = settings_for(database)
    unrelated = resolve_or_provision_identity(
        settings,
        ProviderClaims(provider="google", issuer="issuer", subject="unrelated"),
    )
    unrelated_session = replace_application_session(settings, unrelated.account_id, None)

    with TestClient(create_app(settings)) as client:
        headers = control_headers(client)
        client.post("/api/v1/test-control/create", headers=headers)
        fixture_token = client.cookies[SESSION_COOKIE_NAME]
        project = client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(uuid4()), "title": "Disposable"},
            headers={"Origin": ORIGIN},
        )
        section = client.post(
            f"/api/v1/projects/{project.json()['id']}/sections",
            json={"idempotencyKey": str(uuid4()), "mediaType": "audio/webm;codecs=opus"},
            headers={"Origin": ORIGIN},
        )
        uploaded = client.put(
            f"/api/v1/audio-assets/{section.json()['audioAssetId']}/frames/0",
            content=b"frame",
            headers={
                "Origin": ORIGIN,
                "Content-Type": "audio/webm;codecs=opus",
                "X-Frame-Start-Ms": "0",
                "X-Frame-End-Ms": "1",
            },
        )
        assert uploaded.status_code == 200
        reset = client.post("/api/v1/test-control/reset", headers=headers)
        repeated = client.post("/api/v1/test-control/reset", headers=headers)

    assert reset.status_code == 200
    assert reset.json() == {"status": "reset"}
    assert repeated.json() == {"status": "reset"}
    assert "set-cookie" not in reset.headers
    assert resolve_application_session(settings, fixture_token) is None
    assert resolve_application_session(settings, unrelated_session.token) is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM projects").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM sections").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM audio_assets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM transport_frames").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)


def test_ownership_conflicts_change_nothing(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO users (id, created_at_ms) VALUES (?, 0)",
            (str(FIXTURE_ACCOUNT_ID),),
        )

    with TestClient(create_app(settings_for(database))) as client:
        headers = control_headers(client)
        reset = client.post("/api/v1/test-control/reset", headers=headers)
        create = client.post("/api/v1/test-control/create", headers=headers)

    assert {reset.status_code, create.status_code} == {409}
    assert reset.json()["code"] == "fixture_ownership_conflict"
    assert create.json()["code"] == "fixture_ownership_conflict"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM users WHERE id = ?", (str(FIXTURE_ACCOUNT_ID),)
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM provider_identities").fetchone() == (0,)


def test_control_operations_serialize_concurrent_calls(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    settings = settings_for(database)

    with ThreadPoolExecutor(max_workers=4) as executor:
        sessions = list(executor.map(lambda _: create_fixture(settings), range(8)))

    assert len({session.token for session in sessions}) == 8
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM application_sessions").fetchone() == (8,)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: reset_fixture(settings), range(8)))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_busy_database_returns_retryable_problem_without_partial_change(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    settings = settings_for(database, busy_timeout_ms=25)
    with TestClient(create_app(settings)) as client:
        headers = control_headers(client)
        lock = sqlite3.connect(database, isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            response = client.post("/api/v1/test-control/create", headers=headers)
        finally:
            lock.rollback()
            lock.close()

    assert response.status_code == 503
    assert response.json()["code"] == "database_busy"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)


def test_create_rolls_back_identity_when_session_insertion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    caplog.set_level(logging.ERROR, logger="running_drafts.test_control")

    def fail_session(*_args, **_kwargs):
        raise RuntimeError("private-transaction-failure")

    monkeypatch.setattr("running_drafts.test_control.insert_application_session", fail_session)
    with TestClient(create_app(settings_for(database))) as client:
        headers = control_headers(client)
        response = client.post("/api/v1/test-control/create", headers=headers)

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["referenceId"]
    assert "private-transaction-failure" not in response.text
    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "private-transaction-failure" not in rendered
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM provider_identities").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM application_sessions").fetchone() == (0,)


def test_controls_work_in_production_when_configured(tmp_path: Path) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    settings = settings_for(database, environment=AppEnvironment.PRODUCTION)

    with TestClient(create_app(settings), base_url="https://testserver") as client:
        headers = control_headers(client)
        response = client.post("/api/v1/test-control/create", headers=headers)

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_audit_logs_do_not_disclose_credentials(
    tmp_path: Path,
    caplog,
) -> None:
    database = tmp_path / "application.sqlite3"
    migrate_database(database)
    caplog.set_level(logging.INFO, logger="running_drafts.test_control")

    with TestClient(create_app(settings_for(database))) as client:
        headers = control_headers(client)
        client.post("/api/v1/test-control/create", headers=headers)
        client.post("/api/v1/test-control/reset", headers=headers)
        client.post("/api/v1/test-control/reset", headers={CONTROL_HEADER: "wrong-secret"})

    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert BOOTSTRAP not in rendered
    assert DERIVATION_KEY not in rendered
    assert headers[CONTROL_HEADER] not in rendered
    assert "wrong-secret" not in rendered
    events = [json.loads(JsonFormatter().format(record)) for record in caplog.records]
    assert {(event["operation"], event["outcome"]) for event in events} >= {
        ("challenge", "succeeded"),
        ("create", "succeeded"),
        ("reset", "succeeded"),
        ("reset", "unauthorized"),
    }
