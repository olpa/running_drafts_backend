from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MAX_TRANSPORT_FRAME_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_STAGED_DOWNLOAD_FILES = 20


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_prefix="",
        extra="forbid",
        populate_by_name=True,
    )

    app_env: AppEnvironment = Field(validation_alias="APP_ENV")
    database_path: Path = Field(validation_alias="DATABASE_PATH")
    database_busy_timeout_ms: int = Field(
        default=5_000,
        gt=0,
        validation_alias="DATABASE_BUSY_TIMEOUT_MS",
    )


class Settings(DatabaseSettings):
    allowed_origins: list[str] = Field(validation_alias="ALLOWED_ORIGINS")
    test_control_bootstrap_credential: SecretStr | None = Field(
        default=None,
        validation_alias="TEST_CONTROL_BOOTSTRAP_CREDENTIAL",
    )
    test_control_derivation_key: SecretStr | None = Field(
        default=None,
        validation_alias="TEST_CONTROL_DERIVATION_KEY",
    )
    public_editor_url: str | None = Field(default=None, validation_alias="PUBLIC_EDITOR_URL")
    google_client_id: str | None = Field(default=None, validation_alias="GOOGLE_CLIENT_ID")
    google_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias="GOOGLE_CLIENT_SECRET",
    )
    google_redirect_uri: str | None = Field(default=None, validation_alias="GOOGLE_REDIRECT_URI")
    session_cookie_secure: bool = Field(default=True, validation_alias="SESSION_COOKIE_SECURE")
    server_host: str = Field(default="0.0.0.0", validation_alias="SERVER_HOST")
    server_port: int = Field(default=8000, ge=1, le=65_535, validation_alias="SERVER_PORT")
    max_transport_frame_bytes: int = Field(
        default=DEFAULT_MAX_TRANSPORT_FRAME_BYTES,
        gt=0,
        validation_alias="MAX_TRANSPORT_FRAME_BYTES",
    )
    max_staged_download_files: int = Field(
        default=DEFAULT_MAX_STAGED_DOWNLOAD_FILES,
        gt=0,
        validation_alias="MAX_STAGED_DOWNLOAD_FILES",
    )

    @field_validator("allowed_origins")
    @classmethod
    def validate_allowed_origins(cls, origins: list[str]) -> list[str]:
        if not origins:
            raise ValueError("at least one allowed origin is required")
        if "*" in origins:
            raise ValueError("wildcard CORS origins are forbidden")
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS origins must be exact HTTP(S) origins")
        return origins

    @field_validator(
        "public_editor_url",
        "google_client_id",
        "google_client_secret",
        "google_redirect_uri",
        "test_control_bootstrap_credential",
        "test_control_derivation_key",
        mode="before",
    )
    @classmethod
    def empty_optional_configuration_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("public_editor_url", "google_redirect_uri")
    @classmethod
    def validate_absolute_https_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError("must be an absolute HTTP(S) URL without a fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_authentication(self) -> Settings:
        configured = (
            self.google_client_id is not None,
            self.google_client_secret is not None
            and bool(self.google_client_secret.get_secret_value()),
        )
        if any(configured) and not all(configured):
            raise ValueError("Google OIDC configuration is incomplete")
        if self.public_editor_url is None:
            raise ValueError("PUBLIC_EDITOR_URL is required")
        if self.public_editor_url not in self.allowed_origins:
            raise ValueError("PUBLIC_EDITOR_URL must be one of ALLOWED_ORIGINS")
        editor_url = urlsplit(self.public_editor_url)
        if editor_url.path not in {"", "/"} or editor_url.query:
            raise ValueError("PUBLIC_EDITOR_URL must be an origin without a path or query")
        if self.app_env is AppEnvironment.PRODUCTION and not self.session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE cannot be disabled in production")
        if self.app_env is AppEnvironment.PRODUCTION and editor_url.scheme != "https":
            raise ValueError("PUBLIC_EDITOR_URL must use HTTPS in production")
        if (
            self.app_env is AppEnvironment.PRODUCTION
            and self.google_redirect_uri is not None
            and urlsplit(self.google_redirect_uri).scheme != "https"
        ):
            raise ValueError("GOOGLE_REDIRECT_URI must use HTTPS in production")
        return self

    @property
    def test_control_enabled(self) -> bool:
        return (
            self.test_control_bootstrap_credential is not None
            and self.test_control_derivation_key is not None
        )

    @property
    def expose_api_documentation(self) -> bool:
        return self.app_env is not AppEnvironment.PRODUCTION
