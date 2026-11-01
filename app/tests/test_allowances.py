import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from running_drafts.account_admin import AccountAdministration, GrantIdConflict
from running_drafts.allowances import Plan
from running_drafts.migrations import migrate
from running_drafts.project_storage import (
    AccountSuspended,
    AudioAssetDurationLimitExceeded,
    AudioStorageLimitExceeded,
    DownloadStorageUnavailable,
    ProjectStorage,
)
from running_drafts.settings import AppEnvironment, DatabaseSettings
from running_drafts.storage_accounting import (
    StorageAccountingInvariantViolation,
    project_stored_audio_bytes,
    release_stored_audio_bytes,
)

ACCOUNT_ID = UUID("00000000-0000-4000-8000-000000000001")


def database(tmp_path: Path) -> Path:
    path = tmp_path / "allowances.sqlite3"
    migrate(DatabaseSettings(app_env=AppEnvironment.TEST, database_path=path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users (id, created_at_ms) VALUES (?, 0)",
            (str(ACCOUNT_ID),),
        )
    return path


def storage_with_asset(path: Path) -> tuple[ProjectStorage, UUID]:
    storage = ProjectStorage(path, busy_timeout_ms=100)
    project = storage.create_project(ACCOUNT_ID, uuid4(), "Allowances").value
    section = storage.create_section(
        ACCOUNT_ID,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value
    return storage, section.audio_asset_id


def test_free_usage_and_operator_grants_are_effective(tmp_path: Path) -> None:
    path = database(tmp_path)
    administration = AccountAdministration(path, busy_timeout_ms=100)
    grant_id = uuid4()

    created = administration.create_grant(
        ACCOUNT_ID,
        grant_id,
        stored_bytes=1_000,
        duration_ms=2_000,
    )
    repeated = administration.create_grant(
        ACCOUNT_ID,
        grant_id,
        stored_bytes=1_000,
        duration_ms=2_000,
    )

    usage = ProjectStorage(path, busy_timeout_ms=100).account_usage(ACCOUNT_ID)
    assert created.after == repeated.after
    assert usage.plan is Plan.FREE
    assert usage.used_storage_bytes == 0
    assert usage.granted_storage_bytes == 1_000
    assert usage.storage_warning_bytes == 107_375_182
    assert usage.storage_limit_bytes == 134_218_728
    assert usage.granted_duration_ms == 2_000
    assert usage.duration_limit_ms == 3_602_000

    with pytest.raises(GrantIdConflict):
        administration.create_grant(
            ACCOUNT_ID,
            grant_id,
            stored_bytes=2_000,
            duration_ms=2_000,
        )


def test_frame_allowances_charge_once_and_storage_wins(tmp_path: Path) -> None:
    path = database(tmp_path)
    storage, asset_id = storage_with_asset(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE users SET stored_audio_bytes = ? WHERE id = ?",
            (134_217_727, str(ACCOUNT_ID)),
        )

    accepted = storage.accept_audio_frame(
        ACCOUNT_ID,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=3_600_000,
        media_type="audio/webm;codecs=opus",
        payload=b"x",
    )
    replayed = storage.accept_audio_frame(
        ACCOUNT_ID,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=3_600_000,
        media_type="audio/webm;codecs=opus",
        payload=b"x",
    )
    assert accepted == replayed
    assert storage.account_usage(ACCOUNT_ID).used_storage_bytes == 134_217_728

    with pytest.raises(AudioStorageLimitExceeded):
        storage.accept_audio_frame(
            ACCOUNT_ID,
            asset_id,
            sequence=1,
            start_ms=3_600_000,
            end_ms=3_600_001,
            media_type="audio/webm;codecs=opus",
            payload=b"y",
        )


def test_duration_limit_and_downgrade_grant(tmp_path: Path) -> None:
    path = database(tmp_path)
    storage, asset_id = storage_with_asset(path)
    with pytest.raises(AudioAssetDurationLimitExceeded):
        storage.accept_audio_frame(
            ACCOUNT_ID,
            asset_id,
            sequence=0,
            start_ms=0,
            end_ms=3_600_001,
            media_type="audio/webm;codecs=opus",
            payload=b"x",
        )

    administration = AccountAdministration(path, busy_timeout_ms=100)
    administration.change_plan(ACCOUNT_ID, Plan.PRO)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE users SET stored_audio_bytes = 500 WHERE id = ?",
            (str(ACCOUNT_ID),),
        )
    administration.change_plan(ACCOUNT_ID, Plan.FREE)
    usage = storage.account_usage(ACCOUNT_ID)
    assert usage.granted_storage_bytes == 500
    assert usage.storage_limit_bytes == 134_218_228
    assert len(storage.list_projects(ACCOUNT_ID)) == 1


def test_suspension_deletes_sessions_and_blocks_domain_operations(tmp_path: Path) -> None:
    path = database(tmp_path)
    storage, _asset_id = storage_with_asset(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO application_sessions (
                token_digest, account_id, created_at_ms, expires_at_ms
            ) VALUES (zeroblob(32), ?, 0, 1)
            """,
            (str(ACCOUNT_ID),),
        )
    administration = AccountAdministration(path, busy_timeout_ms=100)
    administration.suspend(ACCOUNT_ID)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM application_sessions").fetchone() == (0,)
    with pytest.raises(AccountSuspended):
        storage.list_projects(ACCOUNT_ID)

    administration.reinstate(ACCOUNT_ID)
    assert len(storage.list_projects(ACCOUNT_ID)) == 1


def test_exact_retry_bypasses_database_reserve_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = database(tmp_path)
    storage, asset_id = storage_with_asset(path)
    accepted = storage.accept_audio_frame(
        ACCOUNT_ID,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1,
        media_type="audio/webm;codecs=opus",
        payload=b"x",
    )

    def unavailable(*_args, **_kwargs) -> None:
        from running_drafts.filesystem_capacity import StorageReserveUnavailable

        raise StorageReserveUnavailable

    monkeypatch.setattr("running_drafts.project_storage.require_capacity", unavailable)
    replay = storage.accept_audio_frame(
        ACCOUNT_ID,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1,
        media_type="audio/webm;codecs=opus",
        payload=b"x",
    )
    assert replay == accepted
    assert storage.account_usage(ACCOUNT_ID).used_storage_bytes == 1
    assert storage.used_audio_bytes(ACCOUNT_ID) == 1
    with pytest.raises(DownloadStorageUnavailable):
        storage.accept_audio_frame(
            ACCOUNT_ID,
            asset_id,
            sequence=1,
            start_ms=1,
            end_ms=2,
            media_type="audio/webm;codecs=opus",
            payload=b"y",
        )


def test_concurrent_assets_race_by_transaction_order_at_storage_limit(tmp_path: Path) -> None:
    path = database(tmp_path)
    first_storage, first_asset = storage_with_asset(path)
    second_storage, second_asset = storage_with_asset(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE users SET stored_audio_bytes = 134217727 WHERE id = ?",
            (str(ACCOUNT_ID),),
        )

    def accept(storage: ProjectStorage, asset_id: UUID) -> str:
        try:
            storage.accept_audio_frame(
                ACCOUNT_ID,
                asset_id,
                sequence=0,
                start_ms=0,
                end_ms=1,
                media_type="audio/webm;codecs=opus",
                payload=b"x",
            )
        except AudioStorageLimitExceeded:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(accept, first_storage, first_asset),
            executor.submit(accept, second_storage, second_asset),
        )
        outcomes = {future.result() for future in futures}
    assert outcomes == {"accepted", "rejected"}


def test_storage_accounting_invariant_uses_committed_frames_as_source_of_truth(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    storage, asset_id = storage_with_asset(path)
    storage.accept_audio_frame(
        ACCOUNT_ID,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1,
        media_type="audio/webm;codecs=opus",
        payload=b"frame",
    )

    invariant = storage.storage_accounting_invariant(ACCOUNT_ID)
    assert invariant.is_consistent
    assert invariant.account_stored_bytes == 5

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE users SET stored_audio_bytes = 4 WHERE id = ?",
            (str(ACCOUNT_ID),),
        )
    invariant = storage.storage_accounting_invariant(ACCOUNT_ID)
    assert not invariant.is_consistent
    assert invariant.frame_stored_bytes == 5


def test_project_release_accounting_primitive_is_transactional(tmp_path: Path) -> None:
    path = database(tmp_path)
    storage, asset_id = storage_with_asset(path)
    storage.accept_audio_frame(
        ACCOUNT_ID,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1,
        media_type="audio/webm;codecs=opus",
        payload=b"released",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        project_id = UUID(
            connection.execute(
                "SELECT id FROM projects WHERE owner_id = ?",
                (str(ACCOUNT_ID),),
            ).fetchone()[0]
        )
        released = project_stored_audio_bytes(connection, ACCOUNT_ID, project_id)
        release_stored_audio_bytes(connection, ACCOUNT_ID, released)
        connection.execute("DELETE FROM projects WHERE id = ?", (str(project_id),))
        connection.commit()

    assert storage.storage_accounting_invariant(ACCOUNT_ID).is_consistent
    assert storage.account_usage(ACCOUNT_ID).used_storage_bytes == 0

    with (
        sqlite3.connect(path) as connection,
        pytest.raises(StorageAccountingInvariantViolation),
    ):
        release_stored_audio_bytes(connection, ACCOUNT_ID, 1)
