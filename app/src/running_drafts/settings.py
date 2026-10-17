from enum import StrEnum
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MAX_TRANSPORT_FRAME_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_STAGED_DOWNLOAD_FILES = 20


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    BASIC = "basic"
    AUTH0 = "auth0"


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


class BasicIdentitySettings(DatabaseSettings):
    auth_mode: AuthMode = Field(validation_alias="AUTH_MODE")
    basic_auth_usernames: list[str] = Field(
        default_factory=list,
        validation_alias="BASIC_AUTH_USERNAMES",
    )

    @field_validator("basic_auth_usernames")
    @classmethod
    def validate_basic_usernames(cls, usernames: list[str]) -> list[str]:
        if len(usernames) != len(set(usernames)):
            raise ValueError("Basic usernames must be unique")
        for username in usernames:
            if (
                not username
                or ":" in username
                or any(character.isspace() for character in username)
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in username)
            ):
                raise ValueError(
                    "Basic usernames must use printable ASCII without colons or whitespace"
                )
        return usernames


class FixtureSettings(BasicIdentitySettings):
    @model_validator(mode="after")
    def fixture_mode_must_be_basic_and_non_production(self) -> FixtureSettings:
        if self.app_env is AppEnvironment.PRODUCTION:
            raise ValueError("fixtures are forbidden in production")
        if self.auth_mode is not AuthMode.BASIC:
            raise ValueError("fixtures require AUTH_MODE=basic")
        if not self.basic_auth_usernames:
            raise ValueError("fixtures require at least one Basic username")
        return self


class Settings(BasicIdentitySettings):
    allowed_origins: list[str] = Field(validation_alias="ALLOWED_ORIGINS")
    basic_auth_password: SecretStr | None = Field(
        default=None,
        validation_alias="BASIC_AUTH_PASSWORD",
    )
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
        return origins

    @model_validator(mode="after")
    def validate_authentication_mode(self) -> Settings:
        if self.auth_mode is AuthMode.AUTH0:
            return self
        if self.app_env is AppEnvironment.PRODUCTION:
            raise ValueError("AUTH_MODE=basic is forbidden in production")
        if not self.basic_auth_usernames:
            raise ValueError("at least one Basic username is required")
        if self.basic_auth_password is None or not self.basic_auth_password.get_secret_value():
            raise ValueError("a nonempty Basic password is required")
        return self

    @property
    def expose_api_documentation(self) -> bool:
        return self.app_env is not AppEnvironment.PRODUCTION
