from pathlib import Path
from uuid import UUID

from fastapi import APIRouter
from fastapi.testclient import TestClient

from running_drafts.api import create_app
from running_drafts.models import UUID4, CamelCaseRequest, CamelCaseResponse
from running_drafts.settings import AppEnvironment, AuthMode, Settings


class ExampleRequest(CamelCaseRequest):
    project_id: UUID4
    display_name: str


class ExampleResponse(CamelCaseResponse):
    project_id: UUID


def settings_for(database_path: Path, environment: AppEnvironment) -> Settings:
    if environment is AppEnvironment.PRODUCTION:
        return Settings(
            app_env=environment,
            auth_mode=AuthMode.AUTH0,
            database_path=database_path,
            allowed_origins=["https://editor.example"],
            basic_auth_usernames=["test-user"],
            basic_auth_password="unused",
        )
    return Settings(
        app_env=environment,
        auth_mode=AuthMode.BASIC,
        database_path=database_path,
        allowed_origins=["https://editor.test"],
        basic_auth_usernames=["test-user"],
        basic_auth_password="test-password",
    )


def app_with_example_route(settings: Settings):
    app = create_app(settings)
    router = APIRouter(prefix="/api/v1")

    @router.post("/example", response_model=ExampleResponse)
    def example(payload: ExampleRequest) -> ExampleResponse:
        return ExampleResponse(project_id=payload.project_id)

    app.include_router(router)
    return app


def test_requests_are_strict_camel_case_and_uuid4(tmp_path: Path) -> None:
    app = app_with_example_route(settings_for(tmp_path / "missing.sqlite3", AppEnvironment.TEST))
    project_id = "00000000-0000-4000-8000-000000000001"

    with TestClient(app) as client:
        valid = client.post(
            "/api/v1/example",
            json={"projectId": project_id, "displayName": "Example"},
        )
        snake_case = client.post(
            "/api/v1/example",
            json={"project_id": project_id, "display_name": "Example"},
        )
        unknown = client.post(
            "/api/v1/example",
            json={
                "projectId": project_id,
                "displayName": "Example",
                "ignored": True,
            },
        )
        wrong_uuid_version = client.post(
            "/api/v1/example",
            json={
                "projectId": "00000000-0000-1000-8000-000000000001",
                "displayName": "Example",
            },
        )

    assert valid.status_code == 200
    assert valid.json() == {"projectId": project_id}
    for response in (snake_case, unknown, wrong_uuid_version):
        assert response.status_code == 422
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "request_validation_error"
        assert set(response.json()["errors"][0]) == {"location", "message", "type"}


def test_cors_allows_only_the_configured_origin(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path / "missing.sqlite3", AppEnvironment.TEST))

    with TestClient(app) as client:
        allowed = client.options(
            "/health/live",
            headers={
                "Origin": "https://editor.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = client.options(
            "/health/live",
            headers={
                "Origin": "https://attacker.test",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://editor.test"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_docs_are_disabled_in_production_but_openapi_remains_testable(
    tmp_path: Path,
) -> None:
    development = create_app(
        settings_for(tmp_path / "development.sqlite3", AppEnvironment.DEVELOPMENT)
    )
    production = create_app(
        settings_for(tmp_path / "production.sqlite3", AppEnvironment.PRODUCTION)
    )

    assert development.docs_url == "/docs"
    assert development.openapi_url == "/openapi.json"
    assert production.docs_url is None
    assert production.openapi_url is None
    assert production.openapi()["openapi"].startswith("3.")


def test_unexpected_failures_use_problem_details(tmp_path: Path) -> None:
    app = create_app(settings_for(tmp_path / "missing.sqlite3", AppEnvironment.TEST))

    @app.get("/api/v1/failure")
    def failure() -> None:
        raise RuntimeError("private failure detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/failure")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal server error",
        "status": 500,
        "detail": "The server could not complete the request.",
        "code": "internal_error",
    }
    assert "private failure detail" not in response.text
