from dataclasses import dataclass

from running_drafts.database import existing_connection
from running_drafts.migrations import MIGRATION_HEAD
from running_drafts.settings import DatabaseSettings


class MaintenanceError(RuntimeError):
    """The requested database maintenance cannot be performed safely."""


@dataclass(frozen=True)
class VacuumResult:
    pages_before: int
    pages_after: int

    @property
    def reclaimed_pages(self) -> int:
        return self.pages_before - self.pages_after


def incremental_vacuum(settings: DatabaseSettings, *, pages: int) -> VacuumResult:
    if pages <= 0:
        raise MaintenanceError("incremental vacuum pages must be greater than zero")
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != MIGRATION_HEAD:
            raise MaintenanceError(
                f"database schema version is {version}, expected {MIGRATION_HEAD}"
            )
        mode = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        if mode != 2:
            raise MaintenanceError("database does not use incremental auto-vacuum")
        pages_before = connection.execute("PRAGMA freelist_count").fetchone()[0]
        connection.execute(f"PRAGMA incremental_vacuum({pages})")
        pages_after = connection.execute("PRAGMA freelist_count").fetchone()[0]
    return VacuumResult(pages_before, pages_after)
