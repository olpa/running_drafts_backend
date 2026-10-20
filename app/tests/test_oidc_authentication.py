import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import ValidationError

from running_drafts.api import create_app
from running_drafts.application_sessions import (
    SESSION_LIFETIME_MS,
    end_application_session,
    replace_application_session,
    resolve_application_session,
)
from running_drafts.auth import SESSION_COOKIE_NAME
from running_drafts.auth_routes import OAUTH_STATE_COOKIE_NAME
from running_drafts.migrations import migrate
from running_drafts.oauth import (
    GoogleOIDCProvider,
    OIDCCancelled,
    OIDCInvalidResponse,
    OIDCTemporaryFailure,
    ProviderClaims,
    ProviderRegistry,
)
from running_drafts.oauth_identities import resolve_or_provision_identity
from running_drafts.oauth_transactions import (
    LOGIN_ATTEMPT_LIFETIME_MS,
    create_login_attempt,
)
from running_drafts.settings import AppEnvironment, AuthMode, DatabaseSettings, Settings
from running_drafts.structured_logging import JsonFormatter


class FakeGoogleProvider:
    name = "google"
    display_name = "Google"

    def __init__(self) -> None:
        self.claims = ProviderClaims(
            provider="google",
            issuer="https://accounts.google.com",
            subject="subject-1",
            email="person@example.test",
            email_verified=True,
            display_name="Test Person",
        )
        self.unavailable = False
        self.last_authorization: dict[str, str] | None = None

    async def authorization_url(self, **parameters: str) -> str:
        if self.unavailable:
            raise OIDCTemporaryFailure("unavailable")
        self.last_authorization = parameters
        return f"https://provider.test/authorize?state={parameters['state']}"

    async def authenticate(
        self,
        *,
        callback_parameters: dict[str, str],
        nonce: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> ProviderClaims:
        assert nonce
        assert code_verifier
        assert redirect_uri == "https://api.test/auth/callback/google"
        if callback_parameters.get("error") == "access_denied":
            raise OIDCCancelled("cancelled")
        if callback_parameters.get("error"):
            raise OIDCInvalidResponse("provider rejected provider-payload-secret")
        if self.unavailable:
            raise OIDCTemporaryFailure("unavailable")
        assert callback_parameters["code"] == "provider-code"
        return self.claims


def oidc_settings(database_path: Path, **overrides: object) -> Settings:
    values = {
        "app_env": AppEnvironment.TEST,
        "auth_mode": AuthMode.OIDC,
        "database_path": database_path,
        "allowed_origins": ["https://editor.test"],
        "public_editor_url": "https://editor.test",
        "google_client_id": "client-id",
        "google_client_secret": "client-secret",
        "google_redirect_uri": "https://api.test/auth/callback/google",
        "session_cookie_secure": False,
    }
    return Settings(**(values | overrides))


def migrated_database(tmp_path: Path) -> Path:
    path = tmp_path / "application.sqlite3"
    migrate(DatabaseSettings(app_env=AppEnvironment.TEST, database_path=path))
    return path


def begin_login(client: TestClient, return_to: str = "/projects") -> str:
    response = client.get(
        "/auth/login/google",
        params={"return_to": return_to},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def complete_login(client: TestClient, state: str):
    return client.get(
        "/auth/callback/google",
        params={"state": state, "code": "provider-code"},
        follow_redirects=False,
    )


def _transient_attempt_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        return connection.execute("SELECT count(*) FROM oidc_login_attempts").fetchone()[0]


def _assert_state_cookie_cleared(response: httpx.Response) -> None:
    cookie = next(
        value
        for value in response.headers.get_list("set-cookie")
        if OAUTH_STATE_COOKIE_NAME in value
    )
    assert "Max-Age=0" in cookie


def test_oidc_configuration_absent_complete_partial_and_redacted(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    disabled = oidc_settings(
        database_path,
        google_client_id=None,
        google_client_secret=None,
    )
    assert disabled.google_client_id is None
    assert create_app(disabled).state.provider_registry.configured() == ()

    configured = oidc_settings(database_path)
    assert configured.google_client_secret.get_secret_value() == "client-secret"

    with pytest.raises(ValidationError) as error:
        oidc_settings(database_path, google_client_secret=None)
    assert "incomplete" in str(error.value)
    assert "client-id" not in str(error.value)

    with pytest.raises(ValidationError) as secret_error:
        oidc_settings(
            database_path,
            google_client_id=None,
            google_client_secret="must-not-appear",
        )
    assert "incomplete" in str(secret_error.value)
    assert "must-not-appear" not in str(secret_error.value)

    with pytest.raises(ValidationError):
        Settings(
            app_env=AppEnvironment.PRODUCTION,
            auth_mode=AuthMode.OIDC,
            database_path=database_path,
            allowed_origins=["https://editor.test"],
            public_editor_url="https://editor.test",
            session_cookie_secure=False,
        )


def _signed_id_token(claims: dict[str, object], key: RSAKey) -> str:
    return jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key, algorithms=["RS256"])


def _google_transport(id_token: str, public_key: RSAKey) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://accounts.google.test/discovery":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://accounts.google.com",
                    "authorization_endpoint": "https://accounts.google.test/authorize",
                    "token_endpoint": "https://accounts.google.test/token",
                    "jwks_uri": "https://accounts.google.test/jwks",
                },
            )
        if str(request.url) == "https://accounts.google.test/token":
            assert b"code_verifier=verifier" in request.content
            return httpx.Response(200, json={"id_token": id_token})
        if str(request.url) == "https://accounts.google.test/jwks":
            serialized_key = public_key.as_dict() | {
                "kid": "test-key",
                "use": "sig",
                "alg": "RS256",
            }
            return httpx.Response(200, json={"keys": [serialized_key]})
        raise AssertionError(f"unexpected provider request: {request.url}")

    return httpx.MockTransport(handler)


def _authenticate_google(id_token: str, public_key: RSAKey) -> ProviderClaims:
    provider = GoogleOIDCProvider(
        "client-id",
        "client-secret",
        transport=_google_transport(id_token, public_key),
    )
    return asyncio.run(
        provider.authenticate(
            callback_parameters={"code": "provider-code"},
            nonce="expected-nonce",
            code_verifier="verifier",
            redirect_uri="https://api.test/auth/callback/google",
        )
    )


def test_google_validates_signature_issuer_audience_expiry_and_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "running_drafts.oauth.GOOGLE_DISCOVERY_URL",
        "https://accounts.google.test/discovery",
    )
    private_key = RSAKey.import_key(rsa.generate_private_key(public_exponent=65_537, key_size=2048))
    public_key = RSAKey.import_key(private_key.as_dict())
    now = int(time.time())
    valid = {
        "iss": "https://accounts.google.com",
        "sub": "subject",
        "aud": "client-id",
        "exp": now + 300,
        "iat": now,
        "nonce": "expected-nonce",
    }

    claims = _authenticate_google(_signed_id_token(valid, private_key), public_key)
    assert claims.subject == "subject"
    assert claims.email is None

    invalid_claim_sets = [
        valid | {"iss": "https://issuer.invalid"},
        valid | {"aud": "other-client"},
        valid | {"exp": now - 120},
        valid | {"nonce": "wrong-nonce"},
    ]
    for invalid in invalid_claim_sets:
        with pytest.raises(OIDCInvalidResponse):
            _authenticate_google(_signed_id_token(invalid, private_key), public_key)

    other_key = RSAKey.import_key(rsa.generate_private_key(public_exponent=65_537, key_size=2048))
    with pytest.raises(OIDCInvalidResponse):
        _authenticate_google(_signed_id_token(valid, other_key), public_key)


def test_provider_list_login_callback_me_and_digest_only_storage(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    app = create_app(
        oidc_settings(database_path),
        provider_registry=ProviderRegistry((provider,)),
    )

    with TestClient(app) as client:
        providers = client.get("/api/v1/auth/providers")
        unknown_provider = client.get("/auth/login/unknown", follow_redirects=False)
        client.cookies.set(
            SESSION_COOKIE_NAME,
            "attacker-fixed",
            domain="testserver.local",
            path="/",
        )
        state = begin_login(client, "/projects/one?tab=record")
        assert provider.last_authorization is not None
        assert provider.last_authorization["code_challenge"]
        assert provider.last_authorization["nonce"]
        callback = complete_login(client, state)
        session_token = client.cookies[SESSION_COOKIE_NAME]
        me = client.get("/api/v1/me")

    assert providers.json() == {"providers": [{"id": "google", "displayName": "Google"}]}
    assert unknown_provider.status_code == 404
    assert unknown_provider.json()["code"] == "provider_not_found"
    assert callback.status_code == 303
    assert session_token != "attacker-fixed"
    assert callback.headers["location"] == "https://editor.test/projects/one?tab=record"
    session_cookie = next(
        value for value in callback.headers.get_list("set-cookie") if SESSION_COOKIE_NAME in value
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Domain=" not in session_cookie
    assert me.status_code == 200
    assert me.json()["account"]["id"]
    assert me.json()["provider"] == {"id": "google", "displayName": "Google"}
    assert me.json()["profile"]["email"] == "person@example.test"
    assert me.json()["sessionExpiresAt"].endswith("Z")

    with sqlite3.connect(database_path) as connection:
        stored = connection.execute("SELECT token_digest FROM application_sessions").fetchone()[0]
    assert stored == hashlib.sha256(session_token.encode("ascii")).digest()
    assert session_token.encode("ascii") not in stored


def test_repeat_identity_refreshes_present_claims_without_clearing_omitted(
    tmp_path: Path,
) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    first = ProviderClaims(
        provider="google",
        issuer="https://accounts.google.com",
        subject="stable",
        email="first@example.test",
        display_name="First Name",
    )
    repeated = ProviderClaims(
        provider="google",
        issuer=first.issuer,
        subject=first.subject,
        email="later@example.test",
    )

    first_identity = resolve_or_provision_identity(settings, first, now_ms=1_000)
    repeated_identity = resolve_or_provision_identity(settings, repeated, now_ms=2_000)

    assert repeated_identity.account_id == first_identity.account_id
    assert repeated_identity.claims.email == "later@example.test"
    assert repeated_identity.claims.display_name == "First Name"


def test_equal_email_different_subjects_create_separate_accounts(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    common = {
        "provider": "google",
        "issuer": "https://accounts.google.com",
        "email": "same@example.test",
    }

    first = resolve_or_provision_identity(settings, ProviderClaims(subject="one", **common))
    second = resolve_or_provision_identity(settings, ProviderClaims(subject="two", **common))

    assert first.account_id != second.account_id


def test_concurrent_provisioning_resolves_one_account_without_orphans(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    claims = ProviderClaims(
        provider="google",
        issuer="https://accounts.google.com",
        subject="racing-subject",
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
        identities = list(
            executor.map(lambda _: resolve_or_provision_identity(settings, claims), range(8))
        )

    assert len({identity.account_id for identity in identities}) == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM users").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM provider_identities").fetchone() == (1,)


@pytest.mark.parametrize(
    "return_to",
    ["https://attacker.test/", "//attacker.test/", "/good#fragment", "/bad\\path"],
)
def test_login_rejects_return_path_attacks(tmp_path: Path, return_to: str) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    with TestClient(
        create_app(
            oidc_settings(database_path),
            provider_registry=ProviderRegistry((provider,)),
        )
    ) as client:
        response = client.get(
            "/auth/login/google",
            params={"return_to": return_to},
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_return_path"


def test_callback_is_single_use_and_clears_transient_state(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    with TestClient(
        create_app(
            oidc_settings(database_path),
            provider_registry=ProviderRegistry((provider,)),
        )
    ) as client:
        state = begin_login(client)
        first = complete_login(client, state)
        client.cookies.set("running_drafts_oidc_state", state, path="/auth/callback")
        replay = complete_login(client, state)

    assert first.headers["location"] == "https://editor.test/projects"
    assert replay.headers["location"].endswith("/login?authError=authentication_failed")


@pytest.mark.parametrize(
    "outcome",
    [
        "missing",
        "malformed",
        "mismatched",
        "wrong_provider",
        "cancelled",
        "invalid",
        "success",
    ],
)
def test_every_terminal_callback_clears_login_attempt(
    tmp_path: Path,
    outcome: str,
) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    with TestClient(
        create_app(
            oidc_settings(database_path),
            provider_registry=ProviderRegistry((provider,)),
        )
    ) as client:
        state = begin_login(client)
        assert _transient_attempt_count(database_path) == 1

        if outcome == "missing":
            response = client.get("/auth/callback/google", follow_redirects=False)
        elif outcome == "malformed":
            response = client.get(
                "/auth/callback/google",
                params={"state": "bad", "code": "provider-code"},
                follow_redirects=False,
            )
        elif outcome == "mismatched":
            response = client.get(
                "/auth/callback/google",
                params={"state": "x" * 43, "code": "provider-code"},
                follow_redirects=False,
            )
        elif outcome == "wrong_provider":
            response = client.get(
                "/auth/callback/unknown",
                params={"state": state, "code": "provider-code"},
                follow_redirects=False,
            )
        elif outcome == "cancelled":
            response = client.get(
                "/auth/callback/google",
                params={"state": state, "error": "access_denied"},
                follow_redirects=False,
            )
        elif outcome == "invalid":
            response = client.get(
                "/auth/callback/google",
                params={"state": state, "error": "server_error"},
                follow_redirects=False,
            )
        else:
            response = complete_login(client, state)

    assert response.status_code in {303, 404}
    assert _transient_attempt_count(database_path) == 0
    _assert_state_cookie_cleared(response)


@pytest.mark.parametrize(
    ("failure", "expected_stage"),
    [
        ("provider", "provider_authentication"),
        ("provisioning", "identity_provisioning"),
        ("session", "session_establishment"),
    ],
)
def test_unexpected_callback_failures_redirect_safely_with_reference_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    expected_stage: str,
) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    app = create_app(
        oidc_settings(database_path),
        provider_registry=ProviderRegistry((provider,)),
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private-exception provider-payload-secret")

    async def fail_provider(**kwargs: object) -> ProviderClaims:
        fail(**kwargs)
        raise AssertionError("unreachable")

    if failure == "provider":
        monkeypatch.setattr(provider, "authenticate", fail_provider)
    else:
        failure_target = (
            "replace_application_session"
            if failure == "session"
            else "resolve_or_provision_identity"
        )
        monkeypatch.setattr(f"running_drafts.auth_routes.{failure_target}", fail)
    caplog.set_level(logging.ERROR, logger="running_drafts.auth_routes")

    with TestClient(app) as client:
        state = begin_login(
            client,
            (
                "/projects/private?tag=one&authError=forged&tag=two%20words"
                "&referenceId=old&blank=&authError=again&referenceId=again&tag=%2B"
            ),
        )
        response = complete_login(client, state)

    assert response.status_code == 303
    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "editor.test",
        "/projects/private",
    )
    assert query["tag"] == ["one", "two words", "+"]
    assert query["blank"] == [""]
    assert query["authError"] == ["authentication_failed"]
    reference_id = query["referenceId"][0]
    assert str(UUID(reference_id)) == reference_id
    assert query_items == [
        ("tag", "one"),
        ("tag", "two words"),
        ("blank", ""),
        ("tag", "+"),
        ("authError", "authentication_failed"),
        ("referenceId", reference_id),
    ]
    browser_content = f"{location}\n{response.text}"
    assert "private-exception" not in browser_content
    assert "provider-payload-secret" not in browser_content
    assert "person@example.test" not in browser_content
    assert "subject-1" not in browser_content
    auth_records = [
        record for record in caplog.records if record.name == "running_drafts.auth_routes"
    ]
    assert len(auth_records) == 1
    formatted_records = [JsonFormatter().format(record) for record in auth_records]
    assert all("provider-payload-secret" not in output for output in formatted_records)
    assert all("private-exception" not in output for output in formatted_records)
    assert all("Traceback" not in output for output in formatted_records)
    logged = json.loads(formatted_records[0])
    assert logged["event"] == "unexpected OIDC authentication failure"
    assert logged["provider"] == "google"
    assert logged["stage"] == expected_stage
    assert logged["category"] == "unexpected_exception"
    assert logged["referenceId"] == reference_id
    assert "exception" not in logged
    assert _transient_attempt_count(database_path) == 0
    _assert_state_cookie_cleared(response)


def test_auth_persistence_uses_one_shared_clock_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    fixed_now_ms = 1_700_000_000_000
    monkeypatch.setattr(
        "running_drafts.clock.utc_milliseconds",
        lambda: fixed_now_ms,
    )

    identity = resolve_or_provision_identity(
        settings,
        ProviderClaims(provider="google", issuer="issuer", subject="shared-clock"),
    )
    issued = replace_application_session(settings, identity.account_id, None)
    create_login_attempt(settings, "google", "/projects")

    with sqlite3.connect(database_path) as connection:
        user_created = connection.execute(
            "SELECT created_at_ms FROM users WHERE id = ?",
            (str(identity.account_id),),
        ).fetchone()[0]
        identity_times = connection.execute(
            "SELECT first_login_at_ms, last_login_at_ms FROM provider_identities"
        ).fetchone()
        session_times = connection.execute(
            "SELECT created_at_ms, expires_at_ms FROM application_sessions"
        ).fetchone()
        attempt_times = connection.execute(
            "SELECT created_at_ms, expires_at_ms FROM oidc_login_attempts"
        ).fetchone()

    assert user_created == fixed_now_ms
    assert identity_times == (fixed_now_ms, fixed_now_ms)
    assert session_times == (fixed_now_ms, fixed_now_ms + SESSION_LIFETIME_MS)
    assert attempt_times == (fixed_now_ms, fixed_now_ms + LOGIN_ATTEMPT_LIFETIME_MS)
    assert issued.expires_at_ms == fixed_now_ms + SESSION_LIFETIME_MS


def test_cancellation_and_provider_outage_are_safe_and_existing_session_survives(
    tmp_path: Path,
) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    with TestClient(
        create_app(
            oidc_settings(database_path),
            provider_registry=ProviderRegistry((provider,)),
        )
    ) as client:
        cancelled_state = begin_login(client)
        cancelled = client.get(
            "/auth/callback/google",
            params={"state": cancelled_state, "error": "access_denied"},
            follow_redirects=False,
        )
        state = begin_login(client)
        assert complete_login(client, state).status_code == 303
        provider.unavailable = True
        assert client.get("/api/v1/me").status_code == 200

    assert cancelled.headers["location"].endswith("/login?authError=cancelled")


def test_login_start_provider_outage_returns_retry_and_clears_attempt(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    provider.unavailable = True
    with TestClient(
        create_app(
            oidc_settings(database_path),
            provider_registry=ProviderRegistry((provider,)),
        )
    ) as client:
        response = client.get("/auth/login/google", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?authError=provider_unavailable")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM oidc_login_attempts").fetchone() == (0,)


def test_unexpected_authorization_request_failure_is_contained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    app = create_app(
        oidc_settings(database_path),
        provider_registry=ProviderRegistry((provider,)),
    )

    async def fail_authorization(**parameters: str) -> str:
        assert parameters["state"]
        raise RuntimeError("authorization-sentinel-secret")

    monkeypatch.setattr(provider, "authorization_url", fail_authorization)
    caplog.set_level(logging.ERROR, logger="running_drafts.auth_routes")

    with TestClient(app) as client:
        client.cookies.set(
            OAUTH_STATE_COOKIE_NAME,
            "x" * 43,
            domain="testserver.local",
            path="/auth/callback",
        )
        response = client.get(
            "/auth/login/google",
            params={
                "return_to": (
                    "/projects?tag=one&authError=forged&tag=two%20words"
                    "&referenceId=old&blank=&authError=again&referenceId=again"
                )
            },
            follow_redirects=False,
        )
        assert OAUTH_STATE_COOKIE_NAME not in client.cookies

    assert response.status_code == 303
    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "editor.test",
        "/projects",
    )
    assert query["tag"] == ["one", "two words"]
    assert query["blank"] == [""]
    assert query["authError"] == ["authentication_failed"]
    reference_id = query["referenceId"][0]
    assert str(UUID(reference_id)) == reference_id
    assert query_items == [
        ("tag", "one"),
        ("tag", "two words"),
        ("blank", ""),
        ("authError", "authentication_failed"),
        ("referenceId", reference_id),
    ]
    browser_content = f"{location}\n{response.text}"
    assert "authorization-sentinel-secret" not in browser_content
    assert "Traceback" not in browser_content
    assert _transient_attempt_count(database_path) == 0
    _assert_state_cookie_cleared(response)

    auth_records = [
        record for record in caplog.records if record.name == "running_drafts.auth_routes"
    ]
    assert len(auth_records) == 1
    formatted_records = [JsonFormatter().format(record) for record in auth_records]
    assert all("authorization-sentinel-secret" not in output for output in formatted_records)
    assert all("Traceback" not in output for output in formatted_records)
    logged = json.loads(formatted_records[0])
    assert logged == {
        "timestamp": logged["timestamp"],
        "level": "error",
        "logger": "running_drafts.auth_routes",
        "event": "unexpected OIDC authentication failure",
        "provider": "google",
        "stage": "authorization_request",
        "category": "unexpected_exception",
        "referenceId": reference_id,
    }


@pytest.mark.parametrize("origin", [None, "https://attacker.test", "null"])
def test_cookie_authenticated_mutations_require_allowed_origin(
    tmp_path: Path,
    origin: str | None,
) -> None:
    database_path = migrated_database(tmp_path)
    provider = FakeGoogleProvider()
    with TestClient(
        create_app(
            oidc_settings(database_path),
            provider_registry=ProviderRegistry((provider,)),
        )
    ) as client:
        state = begin_login(client)
        complete_login(client, state)
        headers = {} if origin is None else {"Origin": origin}
        response = client.post("/auth/logout", headers=headers)

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"


def test_logout_tampering_and_absolute_expiry(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    identity = resolve_or_provision_identity(
        settings,
        ProviderClaims(provider="google", issuer="issuer", subject="subject"),
        now_ms=1_000,
    )
    issued = replace_application_session(
        settings,
        identity.account_id,
        None,
        now_ms=10_000,
    )
    assert (
        resolve_application_session(
            settings,
            issued.token,
            now_ms=10_000 + SESSION_LIFETIME_MS - 1,
        )
        is not None
    )
    assert (
        resolve_application_session(
            settings,
            issued.token,
            now_ms=10_000 + SESSION_LIFETIME_MS,
        )
        is None
    )
    assert resolve_application_session(settings, f"{issued.token}x", now_ms=10_001) is None

    provider = FakeGoogleProvider()
    with TestClient(
        create_app(settings, provider_registry=ProviderRegistry((provider,)))
    ) as client:
        state = begin_login(client)
        complete_login(client, state)
        token = client.cookies[SESSION_COOKIE_NAME]
        logout = client.post("/auth/logout", headers={"Origin": "https://editor.test"})
        me = client.get("/api/v1/me")

    assert logout.status_code == 204
    assert resolve_application_session(settings, token) is None
    assert me.status_code == 401
    assert me.json()["code"] == "authentication_required"


def test_sessions_are_concurrent_local_and_cascade_with_account_deletion(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    identity = resolve_or_provision_identity(
        settings,
        ProviderClaims(provider="google", issuer="issuer", subject="subject"),
    )
    first = replace_application_session(settings, identity.account_id, None)
    second = replace_application_session(settings, identity.account_id, None)

    end_application_session(settings, first.token)

    assert resolve_application_session(settings, first.token) is None
    assert resolve_application_session(settings, second.token) is not None
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM users WHERE id = ?", (str(identity.account_id),))
    assert resolve_application_session(settings, second.token) is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_identities").fetchone() == (0,)


def test_oidc_sessions_preserve_private_project_ownership(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    settings = oidc_settings(database_path)
    first = resolve_or_provision_identity(
        settings,
        ProviderClaims(provider="google", issuer="issuer", subject="first"),
    )
    second = resolve_or_provision_identity(
        settings,
        ProviderClaims(provider="google", issuer="issuer", subject="second"),
    )
    first_session = replace_application_session(settings, first.account_id, None)
    second_session = replace_application_session(settings, second.account_id, None)
    app = create_app(settings, provider_registry=ProviderRegistry())

    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_client.cookies.set(
            SESSION_COOKIE_NAME,
            first_session.token,
            domain="testserver.local",
            path="/",
        )
        second_client.cookies.set(
            SESSION_COOKIE_NAME,
            second_session.token,
            domain="testserver.local",
            path="/",
        )
        created = first_client.post(
            "/api/v1/projects",
            json={"idempotencyKey": str(uuid4()), "title": "Private"},
            headers={"Origin": "https://editor.test"},
        )
        unowned = second_client.get(f"/api/v1/projects/{created.json()['id']}")

    assert created.status_code == 201
    assert unowned.status_code == 404
    assert unowned.json()["code"] == "resource_not_found"
