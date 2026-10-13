from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import Field, field_validator

from running_drafts.auth import authenticated_user_id
from running_drafts.models import UUID4, CamelCaseRequest, CamelCaseResponse
from running_drafts.problems import ProblemDetails
from running_drafts.project_storage import (
    AudioAssetSummary,
    InvalidTitle,
    ProjectDetail,
    ProjectStorage,
    ProjectSummary,
    SectionCreation,
    UnsupportedMediaType,
    canonicalize_media_type,
    normalize_title,
)
from running_drafts.settings import Settings


class CreateProjectRequest(CamelCaseRequest):
    idempotency_key: UUID4
    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        try:
            return normalize_title(value)
        except InvalidTitle as error:
            raise ValueError(str(error)) from None


class RenameProjectRequest(CamelCaseRequest):
    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        try:
            return normalize_title(value)
        except InvalidTitle as error:
            raise ValueError(str(error)) from None


class CreateSectionRequest(CamelCaseRequest):
    idempotency_key: UUID4
    media_type: str

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        try:
            return canonicalize_media_type(value)
        except UnsupportedMediaType as error:
            raise ValueError(str(error)) from None


class CompleteAudioAssetRequest(CamelCaseRequest):
    frame_count: int = Field(ge=0, strict=True)


class ProjectSummaryResponse(CamelCaseResponse):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class AudioAssetSummaryResponse(CamelCaseResponse):
    id: UUID
    media_type: str
    is_sealed: bool
    seal_reason: Literal["completed", "interrupted"] | None
    sealed_at: datetime | None
    frame_count: int
    observed_duration_ms: int
    stored_bytes: int


class SectionSummaryResponse(CamelCaseResponse):
    id: UUID
    position: int
    audio_asset: AudioAssetSummaryResponse


class DocumentResponse(CamelCaseResponse):
    sections: list[SectionSummaryResponse]


class ProjectDetailResponse(ProjectSummaryResponse):
    document: DocumentResponse


class ProjectListResponse(CamelCaseResponse):
    projects: list[ProjectSummaryResponse]


class CreateSectionResponse(CamelCaseResponse):
    section_id: UUID
    audio_asset_id: UUID
    position: int


def _datetime(timestamp_ms: int | None) -> datetime | None:
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1_000, UTC)


def _project_response(project: ProjectSummary) -> ProjectSummaryResponse:
    created_at = _datetime(project.created_at_ms)
    updated_at = _datetime(project.updated_at_ms)
    assert created_at is not None
    assert updated_at is not None
    return ProjectSummaryResponse(
        id=project.id,
        title=project.title,
        created_at=created_at,
        updated_at=updated_at,
    )


def _audio_asset_response(asset: AudioAssetSummary) -> AudioAssetSummaryResponse:
    return AudioAssetSummaryResponse(
        id=asset.id,
        media_type=asset.media_type,
        is_sealed=asset.is_sealed,
        seal_reason=asset.seal_reason,
        sealed_at=_datetime(asset.sealed_at_ms),
        frame_count=asset.frame_count,
        observed_duration_ms=asset.observed_duration_ms,
        stored_bytes=asset.stored_bytes,
    )


def _project_detail_response(detail: ProjectDetail) -> ProjectDetailResponse:
    project = _project_response(detail.project)
    return ProjectDetailResponse(
        **project.model_dump(),
        document=DocumentResponse(
            sections=[
                SectionSummaryResponse(
                    id=section.id,
                    position=section.position,
                    audio_asset=_audio_asset_response(section.audio_asset),
                )
                for section in detail.sections
            ]
        ),
    )


def _section_creation_response(section: SectionCreation) -> CreateSectionResponse:
    return CreateSectionResponse(
        section_id=section.section_id,
        audio_asset_id=section.audio_asset_id,
        position=section.position,
    )


def project_storage(request: Request) -> ProjectStorage:
    settings: Settings = request.app.state.settings
    return ProjectStorage(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    )


AuthenticatedUser = Annotated[UUID, Depends(authenticated_user_id)]
Storage = Annotated[ProjectStorage, Depends(project_storage)]


def _problem_response(
    description: str,
    *,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": ProblemDetails.model_json_schema(),
            }
        },
    }
    if headers is not None:
        response["headers"] = headers
    return response


COMMON_RESPONSES = {
    401: _problem_response(
        "Authentication required",
        headers={
            "WWW-Authenticate": {
                "description": "Authentication challenge",
                "schema": {"type": "string"},
            }
        },
    ),
    422: _problem_response("Request validation failed"),
    503: _problem_response("Database unavailable"),
}
NOT_FOUND_RESPONSES = COMMON_RESPONSES | {
    404: _problem_response("Resource not found"),
}
CONFLICT_RESPONSES = NOT_FOUND_RESPONSES | {
    409: _problem_response("Command conflicts with existing state"),
}
CREATED_RESPONSE = {
    "headers": {
        "Location": {
            "description": "URI of the resource containing the created result",
            "schema": {"type": "string"},
        }
    }
}

router = APIRouter(prefix="/api/v1", tags=["projects"])


@router.post(
    "/projects",
    response_model=ProjectSummaryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={201: CREATED_RESPONSE, **COMMON_RESPONSES, 409: _problem_response("Conflict")},
)
def create_project(
    payload: CreateProjectRequest,
    response: Response,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> ProjectSummaryResponse:
    result = storage.create_project(user_id, payload.idempotency_key, payload.title)
    response.headers["Location"] = f"/api/v1/projects/{result.value.id}"
    return _project_response(result.value)


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    responses=COMMON_RESPONSES,
)
def list_projects(user_id: AuthenticatedUser, storage: Storage) -> ProjectListResponse:
    return ProjectListResponse(
        projects=[_project_response(project) for project in storage.list_projects(user_id)]
    )


@router.get(
    "/projects/{project_id}",
    response_model=ProjectDetailResponse,
    responses=NOT_FOUND_RESPONSES,
)
def get_project(
    project_id: UUID4,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> ProjectDetailResponse:
    return _project_detail_response(storage.get_project(user_id, project_id))


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectSummaryResponse,
    responses=NOT_FOUND_RESPONSES,
)
def rename_project(
    project_id: UUID4,
    payload: RenameProjectRequest,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> ProjectSummaryResponse:
    return _project_response(storage.rename_project(user_id, project_id, payload.title))


@router.post(
    "/projects/{project_id}/sections",
    response_model=CreateSectionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={201: CREATED_RESPONSE, **CONFLICT_RESPONSES},
)
def create_section(
    project_id: UUID4,
    payload: CreateSectionRequest,
    response: Response,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> CreateSectionResponse:
    result = storage.create_section(
        user_id,
        project_id,
        payload.idempotency_key,
        payload.media_type,
    )
    response.headers["Location"] = f"/api/v1/projects/{project_id}"
    return _section_creation_response(result.value)


@router.post(
    "/audio-assets/{audio_asset_id}/complete",
    response_model=AudioAssetSummaryResponse,
    responses=CONFLICT_RESPONSES,
)
def complete_audio_asset(
    audio_asset_id: UUID4,
    payload: CompleteAudioAssetRequest,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> AudioAssetSummaryResponse:
    return _audio_asset_response(
        storage.complete_audio_asset(user_id, audio_asset_id, payload.frame_count)
    )


@router.post(
    "/audio-assets/{audio_asset_id}/interrupt",
    response_model=AudioAssetSummaryResponse,
    responses=CONFLICT_RESPONSES,
)
def interrupt_audio_asset(
    audio_asset_id: UUID4,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> AudioAssetSummaryResponse:
    return _audio_asset_response(storage.interrupt_audio_asset(user_id, audio_asset_id))
