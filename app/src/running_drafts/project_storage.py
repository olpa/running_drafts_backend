import errno
import hashlib
import re
import sqlite3
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import UUID, uuid4

from running_drafts.database import DatabaseConfigurationError, existing_connection

CANONICAL_MEDIA_TYPE = "audio/webm;codecs=opus"
SealReason = Literal["completed", "interrupted"]
_MEDIA_PARAMETER = re.compile(
    r"^([!#$%&'*+.^_`|~0-9A-Za-z-]+)\s*=\s*(?:\"([^\"]*)\"|([^\s;]+))$"
)


class InvalidTitle(ValueError):
    pass


class UnsupportedMediaType(ValueError):
    pass


class ResourceNotFound(Exception):
    pass


class IdempotencyKeyReused(Exception):
    pass


class AudioAssetSealConflict(Exception):
    pass


class AudioAssetNotSealed(Exception):
    pass


class AudioAssemblyFailure(Exception):
    def __init__(
        self,
        audio_asset_id: UUID,
        *,
        expected_frame_count: int,
        actual_frame_count: int,
        expected_bytes: int,
        actual_bytes: int,
        offending_sequence: int | None,
        nearby_hashes: tuple[str, ...],
    ) -> None:
        super().__init__("stored audio frames do not match asset accounting")
        self.audio_asset_id = audio_asset_id
        self.expected_frame_count = expected_frame_count
        self.actual_frame_count = actual_frame_count
        self.expected_bytes = expected_bytes
        self.actual_bytes = actual_bytes
        self.offending_sequence = offending_sequence
        self.nearby_hashes = nearby_hashes


class DownloadStorageUnavailable(Exception):
    pass


class TemporaryFileFailure(RuntimeError):
    pass


class FrameConflict(Exception):
    pass


class DatabaseBusy(Exception):
    pass


class DatabaseUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ProjectSummary:
    id: UUID
    title: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class AudioAssetSummary:
    id: UUID
    media_type: str
    is_sealed: bool
    seal_reason: SealReason | None
    sealed_at_ms: int | None
    frame_count: int
    observed_duration_ms: int
    stored_bytes: int


@dataclass(frozen=True)
class SectionSummary:
    id: UUID
    position: int
    audio_asset: AudioAssetSummary


@dataclass(frozen=True)
class ProjectDetail:
    project: ProjectSummary
    sections: tuple[SectionSummary, ...]


@dataclass(frozen=True)
class SectionCreation:
    section_id: UUID
    audio_asset_id: UUID
    position: int


@dataclass(frozen=True)
class FrameAcknowledgement:
    sequence: int
    sha256: str
    next_expected_sequence: int


@dataclass(frozen=True)
class StagedAudioDownload:
    stream: BinaryIO
    project_title: str
    section_position: int
    seal_reason: SealReason
    content_length: int


@dataclass(frozen=True)
class CreationResult[T]:
    value: T
    created: bool


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        raise InvalidTitle("title must not be empty")
    if len(normalized) > 200:
        raise InvalidTitle("title must contain at most 200 Unicode code points")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise InvalidTitle("title must not contain control characters")
    return normalized


def canonicalize_media_type(value: str) -> str:
    pieces = value.split(";")
    if pieces[0].strip().lower() != "audio/webm":
        raise UnsupportedMediaType("mediaType must be WebM audio with the Opus codec")
    parameters: dict[str, str] = {}
    for piece in pieces[1:]:
        match = _MEDIA_PARAMETER.fullmatch(piece.strip())
        if match is None:
            raise UnsupportedMediaType("mediaType must be WebM audio with the Opus codec")
        name = match.group(1).lower()
        value_part = match.group(2) if match.group(2) is not None else match.group(3)
        if name in parameters:
            raise UnsupportedMediaType("mediaType contains a duplicate parameter")
        parameters[name] = value_part.lower()
    if parameters != {"codecs": "opus"}:
        raise UnsupportedMediaType("mediaType must be WebM audio with the Opus codec")
    return CANONICAL_MEDIA_TYPE


def timestamp_to_rfc3339(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1_000, UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000)


def _temporary_file() -> BinaryIO:
    return tempfile.TemporaryFile(mode="w+b")


def _raise_temporary_file_error(error: OSError) -> None:
    if error.errno in {errno.ENOSPC, errno.EDQUOT}:
        raise DownloadStorageUnavailable from error
    raise TemporaryFileFailure from error


def _assembly_failure(
    audio_asset_id: UUID,
    asset: sqlite3.Row,
    *,
    actual_frame_count: int,
    actual_bytes: int,
    offending_sequence: int | None,
    nearby_hashes: tuple[str, ...],
) -> AudioAssemblyFailure:
    return AudioAssemblyFailure(
        audio_asset_id,
        expected_frame_count=asset["next_expected_sequence"],
        actual_frame_count=actual_frame_count,
        expected_bytes=asset["stored_bytes"],
        actual_bytes=actual_bytes,
        offending_sequence=offending_sequence,
        nearby_hashes=nearby_hashes,
    )


def _is_busy(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


class ProjectStorage:
    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int,
        clock_ms: Callable[[], int] = _now_ms,
        new_id: Callable[[], UUID] = uuid4,
        temporary_file_factory: Callable[[], BinaryIO] = _temporary_file,
    ) -> None:
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._clock_ms = clock_ms
        self._new_id = new_id
        self._temporary_file_factory = temporary_file_factory

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with existing_connection(
                self._database_path,
                busy_timeout_ms=self._busy_timeout_ms,
            ) as connection:
                connection.row_factory = sqlite3.Row
                yield connection
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                raise DatabaseBusy from error
            raise DatabaseUnavailable from error
        except sqlite3.IntegrityError:
            raise
        except sqlite3.DatabaseError as error:
            raise DatabaseUnavailable from error
        except (DatabaseConfigurationError, OSError) as error:
            raise DatabaseUnavailable from error

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def create_project(
        self,
        owner_id: UUID,
        creation_key: UUID,
        title: str,
    ) -> CreationResult[ProjectSummary]:
        title = normalize_title(title)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, title, original_title, created_at_ms, updated_at_ms
                FROM projects
                WHERE owner_id = ? AND creation_key = ?
                """,
                (str(owner_id), str(creation_key)),
            ).fetchone()
            if existing is not None:
                if existing["original_title"] != title:
                    raise IdempotencyKeyReused
                return CreationResult(_project_summary(existing), created=False)

            project_id = self._new_id()
            created_at_ms = self._clock_ms()
            connection.execute(
                """
                INSERT INTO projects (
                    id, owner_id, creation_key, original_title, title,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(project_id),
                    str(owner_id),
                    str(creation_key),
                    title,
                    title,
                    created_at_ms,
                    created_at_ms,
                ),
            )
            return CreationResult(
                ProjectSummary(project_id, title, created_at_ms, created_at_ms),
                created=True,
            )

    def list_projects(self, owner_id: UUID) -> tuple[ProjectSummary, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, title, created_at_ms, updated_at_ms
                FROM projects
                WHERE owner_id = ?
                ORDER BY updated_at_ms DESC, id
                """,
                (str(owner_id),),
            ).fetchall()
        return tuple(_project_summary(row) for row in rows)

    def get_project(self, owner_id: UUID, project_id: UUID) -> ProjectDetail:
        with self._connection() as connection:
            project = connection.execute(
                """
                SELECT id, title, created_at_ms, updated_at_ms
                FROM projects
                WHERE id = ? AND owner_id = ?
                """,
                (str(project_id), str(owner_id)),
            ).fetchone()
            if project is None:
                raise ResourceNotFound
            rows = connection.execute(
                """
                SELECT
                    sections.id AS section_id,
                    sections.position,
                    audio_assets.id AS audio_asset_id,
                    audio_assets.media_type,
                    audio_assets.is_sealed,
                    audio_assets.seal_reason,
                    audio_assets.sealed_at_ms,
                    audio_assets.next_expected_sequence,
                    audio_assets.observed_duration_ms,
                    audio_assets.stored_bytes
                FROM sections
                JOIN audio_assets ON audio_assets.section_id = sections.id
                WHERE sections.project_id = ?
                ORDER BY sections.position
                """,
                (str(project_id),),
            ).fetchall()
        sections = tuple(
            SectionSummary(
                id=UUID(row["section_id"]),
                position=row["position"],
                audio_asset=_audio_asset_summary(row),
            )
            for row in rows
        )
        return ProjectDetail(_project_summary(project), sections)

    def rename_project(
        self,
        owner_id: UUID,
        project_id: UUID,
        title: str,
    ) -> ProjectSummary:
        title = normalize_title(title)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, title, created_at_ms, updated_at_ms
                FROM projects
                WHERE id = ? AND owner_id = ?
                """,
                (str(project_id), str(owner_id)),
            ).fetchone()
            if existing is None:
                raise ResourceNotFound
            if existing["title"] == title:
                return _project_summary(existing)
            updated_at_ms = max(self._clock_ms(), existing["created_at_ms"])
            connection.execute(
                "UPDATE projects SET title = ?, updated_at_ms = ? WHERE id = ?",
                (title, updated_at_ms, str(project_id)),
            )
            return ProjectSummary(
                id=project_id,
                title=title,
                created_at_ms=existing["created_at_ms"],
                updated_at_ms=updated_at_ms,
            )

    def create_section(
        self,
        owner_id: UUID,
        project_id: UUID,
        creation_key: UUID,
        media_type: str,
    ) -> CreationResult[SectionCreation]:
        media_type = canonicalize_media_type(media_type)
        with self._transaction() as connection:
            project = connection.execute(
                "SELECT created_at_ms FROM projects WHERE id = ? AND owner_id = ?",
                (str(project_id), str(owner_id)),
            ).fetchone()
            if project is None:
                raise ResourceNotFound
            existing = connection.execute(
                """
                SELECT
                    sections.id AS section_id,
                    sections.position,
                    sections.creation_media_type,
                    audio_assets.id AS audio_asset_id
                FROM sections
                JOIN audio_assets ON audio_assets.section_id = sections.id
                WHERE sections.project_id = ? AND sections.creation_key = ?
                """,
                (str(project_id), str(creation_key)),
            ).fetchone()
            if existing is not None:
                if existing["creation_media_type"] != media_type:
                    raise IdempotencyKeyReused
                return CreationResult(
                    SectionCreation(
                        section_id=UUID(existing["section_id"]),
                        audio_asset_id=UUID(existing["audio_asset_id"]),
                        position=existing["position"],
                    ),
                    created=False,
                )

            position = connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM sections WHERE project_id = ?",
                (str(project_id),),
            ).fetchone()[0]
            section_id = self._new_id()
            audio_asset_id = self._new_id()
            connection.execute(
                """
                INSERT INTO sections (
                    id, project_id, position, creation_key, creation_media_type
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(section_id),
                    str(project_id),
                    position,
                    str(creation_key),
                    media_type,
                ),
            )
            connection.execute(
                "INSERT INTO audio_assets (id, section_id, media_type) VALUES (?, ?, ?)",
                (str(audio_asset_id), str(section_id), media_type),
            )
            updated_at_ms = max(self._clock_ms(), project["created_at_ms"])
            connection.execute(
                "UPDATE projects SET updated_at_ms = ? WHERE id = ?",
                (updated_at_ms, str(project_id)),
            )
            return CreationResult(
                SectionCreation(section_id, audio_asset_id, position),
                created=True,
            )

    def complete_audio_asset(
        self,
        owner_id: UUID,
        audio_asset_id: UUID,
        frame_count: int,
    ) -> AudioAssetSummary:
        return self._seal_audio_asset(
            owner_id,
            audio_asset_id,
            reason="completed",
            frame_count=frame_count,
        )

    def interrupt_audio_asset(
        self,
        owner_id: UUID,
        audio_asset_id: UUID,
    ) -> AudioAssetSummary:
        return self._seal_audio_asset(
            owner_id,
            audio_asset_id,
            reason="interrupted",
            frame_count=None,
        )

    def accept_audio_frame(
        self,
        owner_id: UUID,
        audio_asset_id: UUID,
        *,
        sequence: int,
        start_ms: int,
        end_ms: int,
        media_type: str,
        payload: bytes,
    ) -> FrameAcknowledgement:
        media_type = canonicalize_media_type(media_type)
        frame_sha256 = hashlib.sha256(payload).hexdigest()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT
                    audio_assets.is_sealed,
                    audio_assets.next_expected_sequence,
                    audio_assets.observed_duration_ms,
                    projects.id AS project_id,
                    projects.created_at_ms
                FROM audio_assets
                JOIN sections ON sections.id = audio_assets.section_id
                JOIN projects ON projects.id = sections.project_id
                WHERE audio_assets.id = ? AND projects.owner_id = ?
                """,
                (str(audio_asset_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise ResourceNotFound
            next_expected_sequence = row["next_expected_sequence"]
            if sequence < next_expected_sequence:
                stored = connection.execute(
                    """
                    SELECT start_ms, end_ms, media_type, sha256
                    FROM transport_frames
                    WHERE audio_asset_id = ? AND sequence = ?
                    """,
                    (str(audio_asset_id), sequence),
                ).fetchone()
                if (
                    stored is None
                    or stored["start_ms"] != start_ms
                    or stored["end_ms"] != end_ms
                    or stored["media_type"] != media_type
                    or stored["sha256"] != frame_sha256
                ):
                    raise FrameConflict
                return FrameAcknowledgement(
                    sequence=sequence,
                    sha256=frame_sha256,
                    next_expected_sequence=next_expected_sequence,
                )
            if row["is_sealed"] or sequence > next_expected_sequence:
                raise FrameConflict
            if start_ms != row["observed_duration_ms"]:
                raise FrameConflict

            connection.execute(
                """
                INSERT INTO transport_frames (
                    audio_asset_id, sequence, start_ms, end_ms,
                    media_type, sha256, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(audio_asset_id),
                    sequence,
                    start_ms,
                    end_ms,
                    media_type,
                    frame_sha256,
                    payload,
                ),
            )
            next_expected_sequence += 1
            connection.execute(
                """
                UPDATE audio_assets
                SET next_expected_sequence = ?,
                    observed_duration_ms = ?,
                    stored_bytes = stored_bytes + ?
                WHERE id = ?
                """,
                (next_expected_sequence, end_ms, len(payload), str(audio_asset_id)),
            )
            updated_at_ms = max(self._clock_ms(), row["created_at_ms"])
            connection.execute(
                "UPDATE projects SET updated_at_ms = ? WHERE id = ?",
                (updated_at_ms, row["project_id"]),
            )
            return FrameAcknowledgement(
                sequence=sequence,
                sha256=frame_sha256,
                next_expected_sequence=next_expected_sequence,
            )

    def _seal_audio_asset(
        self,
        owner_id: UUID,
        audio_asset_id: UUID,
        *,
        reason: SealReason,
        frame_count: int | None,
    ) -> AudioAssetSummary:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT
                    audio_assets.id AS audio_asset_id,
                    audio_assets.media_type,
                    audio_assets.is_sealed,
                    audio_assets.seal_reason,
                    audio_assets.sealed_at_ms,
                    audio_assets.next_expected_sequence,
                    audio_assets.observed_duration_ms,
                    audio_assets.stored_bytes,
                    projects.id AS project_id,
                    projects.created_at_ms
                FROM audio_assets
                JOIN sections ON sections.id = audio_assets.section_id
                JOIN projects ON projects.id = sections.project_id
                WHERE audio_assets.id = ? AND projects.owner_id = ?
                """,
                (str(audio_asset_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise ResourceNotFound
            if row["is_sealed"]:
                same_completion = (
                    reason == "completed"
                    and row["seal_reason"] == "completed"
                    and frame_count == row["next_expected_sequence"]
                )
                same_interruption = reason == "interrupted" and row["seal_reason"] == "interrupted"
                if same_completion or same_interruption:
                    return _audio_asset_summary(row)
                raise AudioAssetSealConflict
            if reason == "completed" and frame_count != row["next_expected_sequence"]:
                raise AudioAssetSealConflict

            sealed_at_ms = self._clock_ms()
            connection.execute(
                """
                UPDATE audio_assets
                SET is_sealed = 1, seal_reason = ?, sealed_at_ms = ?
                WHERE id = ?
                """,
                (reason, sealed_at_ms, str(audio_asset_id)),
            )
            updated_at_ms = max(sealed_at_ms, row["created_at_ms"])
            connection.execute(
                "UPDATE projects SET updated_at_ms = ? WHERE id = ?",
                (updated_at_ms, row["project_id"]),
            )
            return AudioAssetSummary(
                id=audio_asset_id,
                media_type=row["media_type"],
                is_sealed=True,
                seal_reason=reason,
                sealed_at_ms=sealed_at_ms,
                frame_count=row["next_expected_sequence"],
                observed_duration_ms=row["observed_duration_ms"],
                stored_bytes=row["stored_bytes"],
            )

    def used_audio_bytes(self, owner_id: UUID) -> int:
        with self._connection() as connection:
            return connection.execute(
                """
                SELECT COALESCE(SUM(audio_assets.stored_bytes), 0)
                FROM projects
                JOIN sections ON sections.project_id = projects.id
                JOIN audio_assets ON audio_assets.section_id = sections.id
                WHERE projects.owner_id = ?
                """,
                (str(owner_id),),
            ).fetchone()[0]

    def stage_audio_download(
        self,
        owner_id: UUID,
        audio_asset_id: UUID,
    ) -> StagedAudioDownload:
        with self._connection() as connection:
            connection.execute("BEGIN")
            asset = connection.execute(
                """
                SELECT
                    projects.title,
                    sections.position,
                    audio_assets.is_sealed,
                    audio_assets.seal_reason,
                    audio_assets.next_expected_sequence,
                    audio_assets.stored_bytes
                FROM audio_assets
                JOIN sections ON sections.id = audio_assets.section_id
                JOIN projects ON projects.id = sections.project_id
                WHERE audio_assets.id = ? AND projects.owner_id = ?
                """,
                (str(audio_asset_id), str(owner_id)),
            ).fetchone()
            if asset is None:
                raise ResourceNotFound
            if not asset["is_sealed"]:
                raise AudioAssetNotSealed

            if asset["next_expected_sequence"] == 0:
                if asset["stored_bytes"] != 0:
                    raise _assembly_failure(
                        audio_asset_id,
                        asset,
                        actual_frame_count=0,
                        actual_bytes=0,
                        offending_sequence=None,
                        nearby_hashes=(),
                    )
                staged = files("running_drafts").joinpath("assets/empty-audio.webm").open("rb")
                staged.seek(0, 2)
                content_length = staged.tell()
                staged.seek(0)
                connection.commit()
                return StagedAudioDownload(
                    stream=staged,
                    project_title=asset["title"],
                    section_position=asset["position"],
                    seal_reason=asset["seal_reason"],
                    content_length=content_length,
                )

            try:
                staged = self._temporary_file_factory()
            except OSError as error:
                _raise_temporary_file_error(error)
            try:
                actual_frame_count = 0
                actual_bytes = 0
                offending_sequence = None
                nearby_hashes: list[str] = []
                for row in connection.execute(
                    """
                    SELECT sequence, sha256, payload
                    FROM transport_frames
                    WHERE audio_asset_id = ?
                    ORDER BY sequence
                    """,
                    (str(audio_asset_id),),
                ):
                    if offending_sequence is None:
                        nearby_hashes.append(row["sha256"])
                        if len(nearby_hashes) > 3:
                            nearby_hashes.pop(0)
                        if row["sequence"] != actual_frame_count:
                            offending_sequence = row["sequence"]
                    actual_frame_count += 1
                    actual_bytes += len(row["payload"])
                    staged.write(row["payload"])
                if (
                    offending_sequence is not None
                    or actual_frame_count != asset["next_expected_sequence"]
                    or actual_bytes != asset["stored_bytes"]
                    or actual_bytes == 0
                ):
                    raise _assembly_failure(
                        audio_asset_id,
                        asset,
                        actual_frame_count=actual_frame_count,
                        actual_bytes=actual_bytes,
                        offending_sequence=offending_sequence,
                        nearby_hashes=tuple(nearby_hashes),
                    )
                staged.flush()
                content_length = staged.tell()
                if content_length != asset["stored_bytes"]:
                    raise _assembly_failure(
                        audio_asset_id,
                        asset,
                        actual_frame_count=actual_frame_count,
                        actual_bytes=content_length,
                        offending_sequence=None,
                        nearby_hashes=tuple(nearby_hashes),
                    )
                staged.seek(0)
                connection.commit()
            except OSError as error:
                with suppress(OSError):
                    staged.close()
                _raise_temporary_file_error(error)
            except BaseException:
                staged.close()
                raise

        return StagedAudioDownload(
            stream=staged,
            project_title=asset["title"],
            section_position=asset["position"],
            seal_reason=asset["seal_reason"],
            content_length=content_length,
        )


def _project_summary(row: sqlite3.Row) -> ProjectSummary:
    return ProjectSummary(
        id=UUID(row["id"]),
        title=row["title"],
        created_at_ms=row["created_at_ms"],
        updated_at_ms=row["updated_at_ms"],
    )


def _audio_asset_summary(row: sqlite3.Row) -> AudioAssetSummary:
    return AudioAssetSummary(
        id=UUID(row["audio_asset_id"]),
        media_type=row["media_type"],
        is_sealed=bool(row["is_sealed"]),
        seal_reason=row["seal_reason"],
        sealed_at_ms=row["sealed_at_ms"],
        frame_count=row["next_expected_sequence"],
        observed_duration_ms=row["observed_duration_ms"],
        stored_bytes=row["stored_bytes"],
    )
