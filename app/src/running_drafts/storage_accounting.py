import sqlite3
from dataclasses import dataclass
from uuid import UUID


class StorageAccountingInvariantViolation(Exception):
    pass


@dataclass(frozen=True)
class StorageAccountingInvariant:
    account_stored_bytes: int
    asset_stored_bytes: int
    frame_stored_bytes: int

    @property
    def is_consistent(self) -> bool:
        return self.account_stored_bytes == self.asset_stored_bytes == self.frame_stored_bytes


def calculate_storage_accounting_invariant(
    connection: sqlite3.Connection,
    account_id: UUID,
) -> StorageAccountingInvariant:
    row = connection.execute(
        """
        SELECT
            users.stored_audio_bytes,
            COALESCE(SUM(audio_assets.stored_bytes), 0),
            COALESCE(SUM((
                SELECT COALESCE(SUM(length(transport_frames.payload)), 0)
                FROM transport_frames
                WHERE transport_frames.audio_asset_id = audio_assets.id
            )), 0)
        FROM users
        LEFT JOIN projects ON projects.owner_id = users.id
        LEFT JOIN sections ON sections.project_id = projects.id
        LEFT JOIN audio_assets ON audio_assets.section_id = sections.id
        WHERE users.id = ?
        GROUP BY users.id
        """,
        (str(account_id),),
    ).fetchone()
    if row is None:
        raise LookupError("account does not exist")
    return StorageAccountingInvariant(*row)


def project_stored_audio_bytes(
    connection: sqlite3.Connection,
    account_id: UUID,
    project_id: UUID,
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(SUM(audio_assets.stored_bytes), 0)
        FROM projects
        LEFT JOIN sections ON sections.project_id = projects.id
        LEFT JOIN audio_assets ON audio_assets.section_id = sections.id
        WHERE projects.id = ? AND projects.owner_id = ?
        GROUP BY projects.id
        """,
        (str(project_id), str(account_id)),
    ).fetchone()
    if row is None:
        raise LookupError("project does not exist")
    return row[0]


def release_stored_audio_bytes(
    connection: sqlite3.Connection,
    account_id: UUID,
    stored_bytes: int,
) -> None:
    if stored_bytes < 0:
        raise ValueError("stored bytes must be non-negative")
    changed = connection.execute(
        """
        UPDATE users
        SET stored_audio_bytes = stored_audio_bytes - ?
        WHERE id = ? AND stored_audio_bytes >= ?
        """,
        (stored_bytes, str(account_id), stored_bytes),
    ).rowcount
    if changed != 1:
        raise StorageAccountingInvariantViolation
