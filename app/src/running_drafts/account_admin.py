import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from running_drafts import clock
from running_drafts.allowances import Plan
from running_drafts.database import existing_connection


class AccountNotFound(Exception):
    pass


class GrantIdConflict(Exception):
    pass


@dataclass(frozen=True)
class Grant:
    id: UUID
    account_id: UUID
    kind: str
    stored_bytes: int
    duration_ms: int


@dataclass(frozen=True)
class MutationResult[T]:
    before: T | None
    after: T | None


class AccountAdministration:
    def __init__(self, database_path: Path, *, busy_timeout_ms: int) -> None:
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms

    def _transaction(self):
        return _AdminTransaction(self._database_path, self._busy_timeout_ms)

    @staticmethod
    def _require_account(connection: sqlite3.Connection, account_id: UUID) -> None:
        exists = connection.execute(
            "SELECT 1 FROM users WHERE id = ?",
            (str(account_id),),
        ).fetchone()
        if exists is None:
            raise AccountNotFound

    def find_by_email(self, email: str) -> tuple[UUID, ...]:
        with existing_connection(
            self._database_path,
            busy_timeout_ms=self._busy_timeout_ms,
        ) as connection:
            rows = connection.execute(
                """
                SELECT account_id
                FROM provider_identities
                WHERE email = ? COLLATE NOCASE
                ORDER BY account_id
                """,
                (email,),
            ).fetchall()
        return tuple(UUID(row[0]) for row in rows)

    def create_grant(
        self,
        account_id: UUID,
        grant_id: UUID,
        *,
        stored_bytes: int,
        duration_ms: int,
    ) -> MutationResult[Grant]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            existing = self._grant_by_id(connection, grant_id)
            requested = Grant(grant_id, account_id, "operator", stored_bytes, duration_ms)
            if existing is not None:
                if existing != requested:
                    raise GrantIdConflict
                return MutationResult(existing, existing)
            now_ms = clock.utc_milliseconds()
            connection.execute(
                """
                INSERT INTO allowance_grants (
                    id, account_id, kind, stored_bytes, duration_ms,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'operator', ?, ?, ?, ?)
                """,
                (str(grant_id), str(account_id), stored_bytes, duration_ms, now_ms, now_ms),
            )
            return MutationResult(None, requested)

    def patch_grant(
        self,
        account_id: UUID,
        grant_id: UUID,
        *,
        stored_bytes: int | None,
        duration_ms: int | None,
    ) -> MutationResult[Grant]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = self._grant_by_id(connection, grant_id)
            if before is None or before.account_id != account_id or before.kind != "operator":
                raise GrantIdConflict
            after = Grant(
                before.id,
                before.account_id,
                before.kind,
                before.stored_bytes if stored_bytes is None else stored_bytes,
                before.duration_ms if duration_ms is None else duration_ms,
            )
            connection.execute(
                """
                UPDATE allowance_grants
                SET stored_bytes = ?, duration_ms = ?, updated_at_ms = ?
                WHERE id = ?
                """,
                (after.stored_bytes, after.duration_ms, clock.utc_milliseconds(), str(grant_id)),
            )
            return MutationResult(before, after)

    def remove_grant(self, account_id: UUID, grant_id: UUID) -> MutationResult[Grant]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = self._grant_by_id(connection, grant_id)
            if before is not None and (
                before.account_id != account_id or before.kind != "operator"
            ):
                raise GrantIdConflict
            connection.execute(
                """
                DELETE FROM allowance_grants
                WHERE id = ? AND account_id = ? AND kind = 'operator'
                """,
                (str(grant_id), str(account_id)),
            )
            return MutationResult(before, None)

    def patch_downgrade_grant(
        self,
        account_id: UUID,
        *,
        stored_bytes: int,
    ) -> MutationResult[Grant]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = self._downgrade_grant(connection, account_id)
            grant_id = before.id if before is not None else uuid4()
            now_ms = clock.utc_milliseconds()
            connection.execute(
                """
                INSERT INTO allowance_grants (
                    id, account_id, kind, stored_bytes, duration_ms,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'downgrade', ?, 0, ?, ?)
                ON CONFLICT(account_id) WHERE kind = 'downgrade'
                DO UPDATE SET stored_bytes = excluded.stored_bytes,
                              updated_at_ms = excluded.updated_at_ms
                """,
                (str(grant_id), str(account_id), stored_bytes, now_ms, now_ms),
            )
            after = Grant(grant_id, account_id, "downgrade", stored_bytes, 0)
            return MutationResult(before, after)

    def remove_downgrade_grant(self, account_id: UUID) -> MutationResult[Grant]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = self._downgrade_grant(connection, account_id)
            connection.execute(
                "DELETE FROM allowance_grants WHERE account_id = ? AND kind = 'downgrade'",
                (str(account_id),),
            )
            return MutationResult(before, None)

    def suspend(self, account_id: UUID) -> MutationResult[int]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = connection.execute(
                "SELECT suspended_at_ms FROM users WHERE id = ?",
                (str(account_id),),
            ).fetchone()[0]
            after = before if before is not None else clock.utc_milliseconds()
            connection.execute(
                "UPDATE users SET suspended_at_ms = ? WHERE id = ?",
                (after, str(account_id)),
            )
            connection.execute(
                "DELETE FROM application_sessions WHERE account_id = ?",
                (str(account_id),),
            )
            return MutationResult(before, after)

    def reinstate(self, account_id: UUID) -> MutationResult[int]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = connection.execute(
                "SELECT suspended_at_ms FROM users WHERE id = ?",
                (str(account_id),),
            ).fetchone()[0]
            connection.execute(
                "UPDATE users SET suspended_at_ms = NULL WHERE id = ?",
                (str(account_id),),
            )
            return MutationResult(before, None)

    def change_plan(self, account_id: UUID, plan: Plan) -> MutationResult[Plan]:
        with self._transaction() as connection:
            self._require_account(connection, account_id)
            before = Plan(
                connection.execute(
                    "SELECT plan FROM users WHERE id = ?",
                    (str(account_id),),
                ).fetchone()[0]
            )
            if before is Plan.PRO and plan is Plan.FREE:
                used_bytes = connection.execute(
                    "SELECT stored_audio_bytes FROM users WHERE id = ?",
                    (str(account_id),),
                ).fetchone()[0]
                existing = self._downgrade_grant(connection, account_id)
                granted_bytes = max(existing.stored_bytes if existing else 0, used_bytes)
                grant_id = existing.id if existing else uuid4()
                now_ms = clock.utc_milliseconds()
                connection.execute(
                    """
                    INSERT INTO allowance_grants (
                        id, account_id, kind, stored_bytes, duration_ms,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, 'downgrade', ?, 0, ?, ?)
                    ON CONFLICT(account_id) WHERE kind = 'downgrade'
                    DO UPDATE SET stored_bytes = MAX(stored_bytes, excluded.stored_bytes),
                                  updated_at_ms = excluded.updated_at_ms
                    """,
                    (str(grant_id), str(account_id), granted_bytes, now_ms, now_ms),
                )
            connection.execute(
                "UPDATE users SET plan = ? WHERE id = ?",
                (plan.value, str(account_id)),
            )
            return MutationResult(before, plan)

    @staticmethod
    def _grant_by_id(connection: sqlite3.Connection, grant_id: UUID) -> Grant | None:
        row = connection.execute(
            """
            SELECT id, account_id, kind, stored_bytes, duration_ms
            FROM allowance_grants
            WHERE id = ?
            """,
            (str(grant_id),),
        ).fetchone()
        return _grant(row) if row is not None else None

    @staticmethod
    def _downgrade_grant(connection: sqlite3.Connection, account_id: UUID) -> Grant | None:
        row = connection.execute(
            """
            SELECT id, account_id, kind, stored_bytes, duration_ms
            FROM allowance_grants
            WHERE account_id = ? AND kind = 'downgrade'
            """,
            (str(account_id),),
        ).fetchone()
        return _grant(row) if row is not None else None


class _AdminTransaction:
    def __init__(self, path: Path, busy_timeout_ms: int) -> None:
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._context = None
        self._connection = None

    def __enter__(self) -> sqlite3.Connection:
        self._context = existing_connection(self._path, busy_timeout_ms=self._busy_timeout_ms)
        self._connection = self._context.__enter__()
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, error_type, error, traceback) -> None:
        assert self._connection is not None and self._context is not None
        if error_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._context.__exit__(error_type, error, traceback)


def _grant(row: sqlite3.Row) -> Grant:
    return Grant(
        id=UUID(row["id"]),
        account_id=UUID(row["account_id"]),
        kind=row["kind"],
        stored_bytes=row["stored_bytes"],
        duration_ms=row["duration_ms"],
    )
