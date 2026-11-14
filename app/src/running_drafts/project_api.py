import re
import unicodedata
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from threading import BoundedSemaphore, Lock
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import Field, field_validator
from starlette.concurrency import run_in_threadpool

from running_drafts.auth import authenticated_user_id
from running_drafts.models import UUID4, CamelCaseRequest, CamelCaseResponse
from running_drafts.problems import ProblemDetails, ProblemException
from running_drafts.project_storage import (
    AudioAssetSummary,
    FrameAcknowledgement,
    InvalidTitle,
    ProjectDetail,
    ProjectStorage,
    ProjectSummary,
    SectionCreation,
    StagedAudioDownload,
    UnsupportedMediaType,
    canonicalize_media_type,
    normalize_title,
)
from running_drafts.request_limits import (
    PlaybackCapacity,
    PlaybackLease,
    RequestLimiter,
    UploadCapacity,
)
from running_drafts.settings import Settings

MAX_SQLITE_INTEGER = 2**63 - 1


class ConcurrentDownloadLimitExceeded(Exception):
    pass


class DownloadCapacity:
    def __init__(self, maximum: int) -> None:
        self._semaphore = BoundedSemaphore(maximum)

    def acquire(self) -> None:
        if not self._semaphore.acquire(blocking=False):
            raise ConcurrentDownloadLimitExceeded

    def release(self) -> None:
        self._semaphore.release()


class StagedDownloadLease:
    def __init__(
        self,
        download: StagedAudioDownload,
        release_capacity: Callable[[], None],
    ) -> None:
        self.download = download
        self._release_capacity = release_capacity
        self._closed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.download.stream.close()
            finally:
                self._release_capacity()


class ManagedDownloadResponse(StreamingResponse):
    def __init__(self, lease: StagedDownloadLease, **kwargs: Any) -> None:
        self._lease = lease
        super().__init__(_download_body(lease), **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.close()


class ManagedPlaybackResponse(StreamingResponse):
    def __init__(self, lease: PlaybackLease, content: Iterator[bytes], **kwargs: Any) -> None:
        self._playback_lease = lease
        super().__init__(content, **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._playback_lease.release()


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


class FrameAcknowledgementResponse(CamelCaseResponse):
    sequence: int = Field(ge=0)
    acknowledged: Literal[True] = True
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_expected_sequence: int = Field(ge=0)


class AudioStorageUsageResponse(CamelCaseResponse):
    used_bytes: int
    granted_bytes: int
    warning_at_bytes: int
    limit_bytes: int


class AudioAssetDurationUsageResponse(CamelCaseResponse):
    granted_ms: int
    warning_at_ms: int
    limit_ms: int


class UsageResponse(CamelCaseResponse):
    plan: str
    audio_storage: AudioStorageUsageResponse
    audio_asset_duration: AudioAssetDurationUsageResponse


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


def _frame_acknowledgement_response(
    acknowledgement: FrameAcknowledgement,
) -> FrameAcknowledgementResponse:
    return FrameAcknowledgementResponse(
        sequence=acknowledgement.sequence,
        sha256=acknowledgement.sha256,
        next_expected_sequence=acknowledgement.next_expected_sequence,
    )


def _frame_problem(status_code: int, title: str, detail: str, code: str) -> ProblemException:
    return ProblemException(status=status_code, title=title, detail=detail, code=code)


def _download_suffix(download: StagedAudioDownload) -> str:
    suffix = f"-section-{download.section_position + 1}"
    if download.seal_reason == "interrupted":
        suffix += "-interrupted"
    return f"{suffix}.webm"


def _download_filename(download: StagedAudioDownload) -> str:
    suffix = _download_suffix(download)
    slug_parts: list[str] = []
    separator_pending = False
    retained_base = False
    for character in unicodedata.normalize("NFC", download.project_title):
        category = unicodedata.category(character)[0]
        if category in {"L", "N"} or (category == "M" and retained_base):
            if separator_pending and slug_parts:
                slug_parts.append("-")
            slug_parts.append(character)
            separator_pending = False
            retained_base = True
        else:
            separator_pending = True
            retained_base = False
    slug = "".join(slug_parts) or "untitled"
    maximum_slug_bytes = 240 - len(suffix.encode())
    while len(slug.encode()) > maximum_slug_bytes:
        slug = slug[:-1]
    slug = slug.rstrip("-") or "untitled"
    return f"{slug}{suffix}"


def _ascii_download_filename(download: StagedAudioDownload, filename: str) -> str:
    if filename.isascii():
        return filename
    return f"download{_download_suffix(download)}"


def _download_body(
    lease: StagedDownloadLease,
) -> Iterator[bytes]:
    try:
        while chunk := lease.download.stream.read(64 * 1024):
            yield chunk
    finally:
        lease.close()


async def _read_frame_body(request: Request, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum_bytes:
                raise _frame_problem(
                    413,
                    "Frame too large",
                    "The audio frame exceeds the configured size limit.",
                    "frame_too_large",
                )
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        remaining = maximum_bytes - len(body)
        if len(chunk) > remaining:
            raise _frame_problem(
                413,
                "Frame too large",
                "The audio frame exceeds the configured size limit.",
                "frame_too_large",
            )
        body.extend(chunk)
    if not body:
        raise _frame_problem(
            422,
            "Request validation failed",
            "The audio frame body must not be empty.",
            "request_validation_error",
        )
    return bytes(body)


def project_storage(request: Request) -> ProjectStorage:
    settings: Settings = request.app.state.settings
    return ProjectStorage(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
        database_min_free_bytes=settings.database_min_free_bytes,
        download_staging_directory=settings.download_staging_directory,
        staging_min_free_bytes=settings.staging_min_free_bytes,
    )


AuthenticatedUser = Annotated[UUID, Depends(authenticated_user_id)]
Storage = Annotated[ProjectStorage, Depends(project_storage)]


def enforce_request_limit(request: Request, user_id: AuthenticatedUser) -> None:
    limiter: RequestLimiter = request.app.state.request_limiter
    limiter.consume(user_id)


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
    403: _problem_response("Account suspended or request origin rejected"),
    422: _problem_response("Request validation failed"),
    429: _problem_response("Request or allowance limit exceeded"),
    503: _problem_response("Database unavailable"),
}
NOT_FOUND_RESPONSES = COMMON_RESPONSES | {
    404: _problem_response("Resource not found"),
}
CONFLICT_RESPONSES = NOT_FOUND_RESPONSES | {
    409: _problem_response("Command conflicts with existing state"),
}
FRAME_RESPONSES = CONFLICT_RESPONSES | {
    413: _problem_response("Frame body exceeds the configured size limit"),
    415: _problem_response("Frame media type is unsupported"),
}
DOWNLOAD_RESPONSES = CONFLICT_RESPONSES | {
    200: {
        "description": "Sealed original audio",
        "content": {
            "audio/webm;codecs=opus": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
        "headers": {
            "Content-Disposition": {
                "description": "Attachment filename",
                "schema": {"type": "string"},
            },
            "Content-Length": {
                "description": "Exact assembled byte length",
                "schema": {"type": "integer"},
            },
        },
    },
    429: _problem_response("Concurrent download limit exceeded"),
    500: _problem_response("Audio assembly failed"),
    503: _problem_response("Database or temporary storage unavailable"),
}
PLAYBACK_RESPONSES = CONFLICT_RESPONSES | {
    200: {
        "description": "Complete immutable playback snapshot",
        "content": {
            "audio/webm;codecs=opus": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
        "headers": {
            name: {"schema": {"type": "string"}}
            for name in ("Accept-Ranges", "Cache-Control", "Content-Length", "ETag")
        },
    },
    206: {
        "description": "Single byte range from an immutable playback snapshot",
        "content": {
            "audio/webm;codecs=opus": {
                "schema": {"type": "string", "format": "binary"},
            }
        },
        "headers": {
            name: {"schema": {"type": "string"}}
            for name in (
                "Accept-Ranges",
                "Cache-Control",
                "Content-Length",
                "Content-Range",
                "ETag",
            )
        },
    },
    304: {"description": "Snapshot validator matched"},
    400: _problem_response("A byte range is required or malformed"),
    416: _problem_response("The requested byte range cannot be satisfied"),
    429: _problem_response("Concurrent playback limit exceeded"),
    500: _problem_response("Playback representation corrupt"),
}
CREATED_RESPONSE = {
    "headers": {
        "Location": {
            "description": "URI of the resource containing the created result",
            "schema": {"type": "string"},
        }
    }
}

router = APIRouter(
    prefix="/api/v1",
    tags=["projects"],
    dependencies=[Depends(enforce_request_limit)],
)


@router.get("/me/usage", response_model=UsageResponse, responses=COMMON_RESPONSES)
def get_usage(user_id: AuthenticatedUser, storage: Storage) -> UsageResponse:
    usage = storage.account_usage(user_id)
    return UsageResponse(
        plan=usage.plan.value,
        audio_storage=AudioStorageUsageResponse(
            used_bytes=usage.used_storage_bytes,
            granted_bytes=usage.granted_storage_bytes,
            warning_at_bytes=usage.storage_warning_bytes,
            limit_bytes=usage.storage_limit_bytes,
        ),
        audio_asset_duration=AudioAssetDurationUsageResponse(
            granted_ms=usage.granted_duration_ms,
            warning_at_ms=usage.duration_warning_ms,
            limit_ms=usage.duration_limit_ms,
        ),
    )


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


@router.put(
    "/audio-assets/{audio_asset_id}/frames/{sequence}",
    response_model=FrameAcknowledgementResponse,
    responses=FRAME_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "audio/webm;codecs=opus": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                        "minLength": 1,
                        "description": "Bounded by MAX_TRANSPORT_FRAME_BYTES.",
                    }
                }
            },
        }
    },
)
async def upload_audio_frame(
    request: Request,
    audio_asset_id: UUID4,
    sequence: Annotated[int, Path(ge=0, le=MAX_SQLITE_INTEGER)],
    start_ms: Annotated[
        int,
        Header(alias="X-Frame-Start-Ms", ge=0, le=MAX_SQLITE_INTEGER),
    ],
    end_ms: Annotated[
        int,
        Header(alias="X-Frame-End-Ms", ge=0, le=MAX_SQLITE_INTEGER),
    ],
    user_id: AuthenticatedUser,
    storage: Storage,
) -> FrameAcknowledgementResponse:
    capacity: UploadCapacity = request.app.state.upload_capacity
    lease = capacity.acquire(user_id)
    try:
        if end_ms <= start_ms:
            raise _frame_problem(
                422,
                "Request validation failed",
                "X-Frame-End-Ms must be greater than X-Frame-Start-Ms.",
                "request_validation_error",
            )
        content_type = request.headers.get("content-type")
        if content_type is None:
            raise _frame_problem(
                415,
                "Unsupported media type",
                "Content-Type must identify WebM audio with the Opus codec.",
                "unsupported_media_type",
            )
        try:
            media_type = canonicalize_media_type(content_type)
        except UnsupportedMediaType as error:
            raise _frame_problem(
                415,
                "Unsupported media type",
                str(error),
                "unsupported_media_type",
            ) from None
        settings: Settings = request.app.state.settings
        payload = await _read_frame_body(request, settings.max_transport_frame_bytes)
        return _frame_acknowledgement_response(
            await run_in_threadpool(
                storage.accept_audio_frame,
                user_id,
                audio_asset_id,
                sequence=sequence,
                start_ms=start_ms,
                end_ms=end_ms,
                media_type=media_type,
                payload=payload,
            )
        )
    finally:
        lease.release()


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


@router.get(
    "/audio-assets/{audio_asset_id}/download",
    response_class=StreamingResponse,
    responses=DOWNLOAD_RESPONSES,
)
def download_audio_asset(
    request: Request,
    audio_asset_id: UUID4,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> StreamingResponse:
    capacity: DownloadCapacity = request.app.state.download_capacity
    capacity.acquire()
    try:
        download = storage.stage_audio_download(user_id, audio_asset_id)
    except BaseException:
        capacity.release()
        raise
    lease = StagedDownloadLease(download, capacity.release)
    try:
        filename = _download_filename(download)
        ascii_filename = _ascii_download_filename(download, filename)
        return ManagedDownloadResponse(
            lease,
            media_type="audio/webm;codecs=opus",
            headers={
                "Content-Length": str(download.content_length),
                "Content-Disposition": (
                    f'attachment; filename="{ascii_filename}"; '
                    f"filename*=UTF-8''{quote(filename, safe='!#$&+-.^_`|~')}"
                ),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except BaseException:
        lease.close()
        raise


def _playback_problem(
    status_code: int,
    title: str,
    detail: str,
    code: str,
    *,
    total: int,
    etag: str,
    include_content_range: bool = False,
) -> ProblemException:
    headers = {"Accept-Ranges": "bytes", "ETag": etag}
    if include_content_range:
        headers["Content-Range"] = f"bytes */{total}"
    return ProblemException(
        status=status_code,
        title=title,
        detail=detail,
        code=code,
        headers=headers,
    )


_SINGLE_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _playback_interval(
    value: str | None,
    *,
    total: int,
    maximum: int,
    etag: str,
) -> tuple[int, int, int]:
    if value is None:
        if total > maximum:
            raise _playback_problem(
                400,
                "Byte range required",
                "This playback representation is too large for an un-ranged response.",
                "range_required",
                total=total,
                etag=etag,
            )
        return 0, total, 200
    if "," in value:
        raise _playback_problem(
            416,
            "Multiple ranges are not supported",
            "Playback supports exactly one byte range per request.",
            "multiple_ranges_not_supported",
            total=total,
            etag=etag,
            include_content_range=True,
        )
    match = _SINGLE_RANGE.fullmatch(value.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise _playback_problem(
            400,
            "Invalid byte range",
            "Range must contain one valid bytes interval.",
            "invalid_range",
            total=total,
            etag=etag,
        )
    start_text, end_text = match.groups()
    if not start_text:
        suffix = int(end_text)
        if suffix == 0:
            raise _playback_problem(
                416,
                "Byte range not satisfiable",
                "The suffix byte range cannot be satisfied.",
                "range_not_satisfiable",
                total=total,
                etag=etag,
                include_content_range=True,
            )
        if suffix > maximum:
            raise _playback_problem(
                416,
                "Byte range too large",
                "The suffix byte range exceeds the playback response limit.",
                "range_too_large",
                total=total,
                etag=etag,
                include_content_range=True,
            )
        return max(0, total - suffix), total, 206

    start = int(start_text)
    if start >= total:
        raise _playback_problem(
            416,
            "Byte range not satisfiable",
            "The byte range begins beyond the representation.",
            "range_not_satisfiable",
            total=total,
            etag=etag,
            include_content_range=True,
        )
    if end_text:
        requested_end = int(end_text)
        if requested_end < start:
            raise _playback_problem(
                416,
                "Byte range not satisfiable",
                "The byte range end precedes its start.",
                "range_not_satisfiable",
                total=total,
                etag=etag,
                include_content_range=True,
            )
        end = min(total, requested_end + 1, start + maximum)
    else:
        end = min(total, start + maximum)
    return start, end, 206


def _etag_matches(value: str, etag: str) -> bool:
    def opaque_tag(tag: str) -> str:
        tag = tag.strip()
        return tag[2:].lstrip() if tag.startswith("W/") else tag

    return value.strip() == "*" or opaque_tag(etag) in {
        opaque_tag(item) for item in value.split(",")
    }


@router.get(
    "/audio-assets/{audio_asset_id}/playback/latest",
    operation_id="get_latest_playback_snapshot",
    responses=NOT_FOUND_RESPONSES | {307: {"description": "Current immutable snapshot"}},
)
@router.head(
    "/audio-assets/{audio_asset_id}/playback/latest",
    operation_id="head_latest_playback_snapshot",
    responses=NOT_FOUND_RESPONSES | {307: {"description": "Current immutable snapshot"}},
)
def latest_playback_snapshot(
    audio_asset_id: UUID4,
    user_id: AuthenticatedUser,
    storage: Storage,
) -> Response:
    frame_count = storage.latest_playback_frame_count(user_id, audio_asset_id)
    return RedirectResponse(
        url=f"/api/v1/audio-assets/{audio_asset_id}/playback/snapshots/{frame_count}",
        status_code=307,
        headers={"Cache-Control": "private, no-store"},
    )


@router.get(
    "/audio-assets/{audio_asset_id}/playback/snapshots/{frame_count}",
    operation_id="get_playback_snapshot",
    responses=PLAYBACK_RESPONSES,
)
@router.head(
    "/audio-assets/{audio_asset_id}/playback/snapshots/{frame_count}",
    operation_id="head_playback_snapshot",
    responses=PLAYBACK_RESPONSES,
)
def playback_snapshot(
    request: Request,
    audio_asset_id: UUID4,
    frame_count: Annotated[int, Path(ge=0, le=MAX_SQLITE_INTEGER)],
    user_id: AuthenticatedUser,
    storage: Storage,
) -> Response:
    snapshot = storage.playback_snapshot(user_id, audio_asset_id, frame_count)
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=31536000, immutable",
        "ETag": snapshot.etag,
        "X-Content-Type-Options": "nosniff",
    }
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None and _etag_matches(if_none_match, snapshot.etag):
        return Response(status_code=304, headers=common_headers)

    requested_range = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if if_range is not None and if_range.strip() != snapshot.etag:
        requested_range = None
    settings: Settings = request.app.state.settings
    if request.method == "HEAD" and requested_range is None:
        start, end, response_status = 0, snapshot.total_length, 200
    else:
        start, end, response_status = _playback_interval(
            requested_range,
            total=snapshot.total_length,
            maximum=settings.max_playback_response_bytes,
            etag=snapshot.etag,
        )
    content_length = end - start
    headers = common_headers | {"Content-Length": str(content_length)}
    if response_status == 206:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{snapshot.total_length}"
    if request.method == "HEAD":
        return Response(
            status_code=response_status,
            media_type="audio/webm;codecs=opus",
            headers=headers,
        )

    capacity: PlaybackCapacity = request.app.state.playback_capacity
    lease = capacity.acquire(user_id)
    try:
        return ManagedPlaybackResponse(
            lease,
            storage.playback_range(snapshot, start, end),
            status_code=response_status,
            media_type="audio/webm;codecs=opus",
            headers=headers,
        )
    except BaseException:
        lease.release()
        raise
