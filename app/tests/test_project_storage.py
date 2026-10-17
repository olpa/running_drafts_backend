import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryFile
from threading import Event
from uuid import UUID, uuid4

import pytest

from running_drafts.database import existing_connection
from running_drafts.project_storage import (
    AudioAssemblyFailure,
    FrameConflict,
    ProjectStorage,
    ResourceNotFound,
)


def run_admin(command: str, database_path: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": "test",
        "AUTH_MODE": "basic",
        "DATABASE_PATH": str(database_path),
        "BASIC_AUTH_USERNAMES": json.dumps(["Alice", "Bob"]),
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def migrated_database(tmp_path: Path) -> Path:
    path = tmp_path / "application.sqlite3"
    assert run_admin("migrate", path).returncode == 0
    assert run_admin("fixtures", path).returncode == 0
    return path


def user_id(database_path: Path, username: str = "Alice") -> UUID:
    with sqlite3.connect(database_path) as connection:
        value = connection.execute(
            "SELECT user_id FROM basic_auth_identities WHERE username = ?",
            (username,),
        ).fetchone()[0]
    return UUID(value)


def storage(database_path: Path, **overrides: object) -> ProjectStorage:
    return ProjectStorage(database_path, busy_timeout_ms=5_000, **overrides)


def accept_test_frame(
    database_path: Path,
    audio_asset_id: UUID,
    *,
    payload: bytes = b"frame",
    ready_to_commit: Event | None = None,
    allow_commit: Event | None = None,
) -> bool:
    with existing_connection(database_path, busy_timeout_ms=5_000) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            asset = connection.execute(
                """
                SELECT is_sealed, next_expected_sequence, observed_duration_ms, stored_bytes
                FROM audio_assets
                WHERE id = ?
                """,
                (str(audio_asset_id),),
            ).fetchone()
            assert asset is not None
            if asset["is_sealed"]:
                connection.rollback()
                return False
            sequence = asset["next_expected_sequence"]
            start_ms = asset["observed_duration_ms"]
            end_ms = start_ms + 1_000
            connection.execute(
                """
                INSERT INTO transport_frames (
                    audio_asset_id, sequence, start_ms, end_ms, media_type, sha256, payload
                ) VALUES (?, ?, ?, ?, 'audio/webm;codecs=opus', ?, ?)
                """,
                (str(audio_asset_id), sequence, start_ms, end_ms, "0" * 64, payload),
            )
            connection.execute(
                """
                UPDATE audio_assets
                SET next_expected_sequence = next_expected_sequence + 1,
                    observed_duration_ms = ?,
                    stored_bytes = stored_bytes + ?
                WHERE id = ?
                """,
                (end_ms, len(payload), str(audio_asset_id)),
            )
            if ready_to_commit is not None:
                ready_to_commit.set()
            if allow_commit is not None:
                assert allow_commit.wait(timeout=5)
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise


def test_state_survives_reopening_the_storage(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    first_process = storage(database_path, clock_ms=lambda: 1_000)
    project = first_process.create_project(owner_id, uuid4(), "Persistent").value
    created = first_process.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value
    first_process.complete_audio_asset(owner_id, created.audio_asset_id, 0)

    restarted_process = storage(database_path)
    detail = restarted_process.get_project(owner_id, project.id)

    assert detail.project.title == "Persistent"
    assert len(detail.sections) == 1
    assert detail.sections[0].audio_asset.is_sealed is True
    assert detail.sections[0].audio_asset.seal_reason == "completed"


def test_first_audio_frame_is_acknowledged_and_survives_restart(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    moments = iter([1_000, 2_000, 3_000])
    store = storage(database_path, clock_ms=lambda: next(moments))
    project = store.create_project(owner_id, uuid4(), "Persistent audio").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id

    acknowledgement = store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame-one",
    )

    assert acknowledgement.sequence == 0
    assert acknowledgement.sha256 == (
        "70a09fbff3df33ed9f6db65155e3a6e166de73e2ceab15d42103d688fc904eaa"
    )
    assert acknowledgement.next_expected_sequence == 1
    restarted_store = storage(database_path)
    restarted_detail = restarted_store.get_project(owner_id, project.id)
    assert restarted_detail.project.updated_at_ms == 3_000
    assert restarted_detail.sections[0].audio_asset.frame_count == 1
    assert restarted_detail.sections[0].audio_asset.observed_duration_ms == 1_000
    assert restarted_detail.sections[0].audio_asset.stored_bytes == 9
    replay = restarted_store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame-one",
    )
    assert replay.next_expected_sequence == 1


def test_audio_frame_replays_are_idempotent_and_return_the_current_cursor(
    tmp_path: Path,
) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path, clock_ms=lambda: 1_000)
    project = store.create_project(owner_id, uuid4(), "Replays").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    first = store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame-one",
    )
    second = store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=1,
        start_ms=1_000,
        end_ms=2_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame-two",
    )
    updated_at_ms = store.get_project(owner_id, project.id).project.updated_at_ms

    replay = store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type=' AUDIO/WEBM ; Codecs = "OPUS" ',
        payload=b"frame-one",
    )

    assert first.next_expected_sequence == 1
    assert second.next_expected_sequence == 2
    assert replay.sha256 == first.sha256
    assert replay.next_expected_sequence == 2
    assert store.get_project(owner_id, project.id).project.updated_at_ms == updated_at_ms

    conflicting_requests = [
        {"sequence": 3, "start_ms": 2_000, "end_ms": 3_000, "payload": b"gap"},
        {"sequence": 0, "start_ms": 0, "end_ms": 1_000, "payload": b"changed"},
        {"sequence": 0, "start_ms": 0, "end_ms": 999, "payload": b"frame-one"},
    ]
    for request in conflicting_requests:
        with pytest.raises(FrameConflict):
            store.accept_audio_frame(
                owner_id,
                asset_id,
                media_type="audio/webm;codecs=opus",
                **request,
            )

    store.complete_audio_asset(owner_id, asset_id, 2)
    sealed_replay = store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame-one",
    )
    assert sealed_replay.next_expected_sequence == 2
    with pytest.raises(FrameConflict):
        store.accept_audio_frame(
            owner_id,
            asset_id,
            sequence=2,
            start_ms=2_000,
            end_ms=3_000,
            media_type="audio/webm;codecs=opus",
            payload=b"new-after-seal",
        )


def test_concurrent_section_creation_assigns_a_stable_total_order(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    project = storage(database_path).create_project(owner_id, uuid4(), "Concurrent").value

    def create_one(_index: int) -> object:
        return storage(database_path).create_section(
            owner_id,
            project.id,
            uuid4(),
            "audio/webm;codecs=opus",
        ).value

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(create_one, range(12)))

    assert sorted(item.position for item in created) == list(range(12))
    detail = storage(database_path).get_project(owner_id, project.id)
    assert [section.position for section in detail.sections] == list(range(12))
    assert len({section.id for section in detail.sections}) == 12


def test_concurrent_creation_replays_return_one_project_and_section(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    project_key = uuid4()

    def create_same_project(_index: int) -> object:
        return storage(database_path).create_project(owner_id, project_key, "Idempotent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        projects = list(executor.map(create_same_project, range(2)))

    assert len({result.value.id for result in projects}) == 1
    assert sorted(result.created for result in projects) == [False, True]
    project_id = projects[0].value.id
    section_key = uuid4()

    def create_same_section(_index: int) -> object:
        return storage(database_path).create_section(
            owner_id,
            project_id,
            section_key,
            "audio/webm;codecs=opus",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        sections = list(executor.map(create_same_section, range(2)))

    assert len({result.value.section_id for result in sections}) == 1
    assert len({result.value.audio_asset_id for result in sections}) == 1
    assert sorted(result.created for result in sections) == [False, True]


def test_concurrent_renames_are_last_commit_wins(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    base = storage(database_path, clock_ms=lambda: 1_000)
    project = base.create_project(owner_id, uuid4(), "Original").value
    first_has_lock = Event()
    release_first = Event()

    def first_clock() -> int:
        first_has_lock.set()
        assert release_first.wait(timeout=5)
        return 2_000

    first = storage(database_path, clock_ms=first_clock)
    second = storage(database_path, clock_ms=lambda: 3_000)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_rename = executor.submit(first.rename_project, owner_id, project.id, "First")
        assert first_has_lock.wait(timeout=5)
        second_rename = executor.submit(second.rename_project, owner_id, project.id, "Second")
        release_first.set()
        assert first_rename.result().title == "First"
        assert second_rename.result().title == "Second"

    assert storage(database_path).get_project(owner_id, project.id).project.title == "Second"


def test_frame_and_seal_transactions_follow_database_order(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Races").value

    frame_first = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value
    frame_ready = Event()
    allow_frame_commit = Event()

    def frame_clock() -> int:
        frame_ready.set()
        assert allow_frame_commit.wait(timeout=5)
        return 3_000

    frame_store = storage(database_path, clock_ms=frame_clock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepting = executor.submit(
            frame_store.accept_audio_frame,
            owner_id,
            frame_first.audio_asset_id,
            sequence=0,
            start_ms=0,
            end_ms=1_000,
            media_type="audio/webm;codecs=opus",
            payload=b"frame",
        )
        assert frame_ready.wait(timeout=5)
        sealing_after_frame = executor.submit(
            store.complete_audio_asset,
            owner_id,
            frame_first.audio_asset_id,
            1,
        )
        allow_frame_commit.set()
        assert accepting.result().next_expected_sequence == 1
        completed = sealing_after_frame.result()
    assert completed.frame_count == 1
    assert completed.observed_duration_ms == 1_000
    assert completed.stored_bytes == 5

    seal_first = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value
    sealing_has_lock = Event()
    release_seal = Event()

    def sealing_clock() -> int:
        sealing_has_lock.set()
        assert release_seal.wait(timeout=5)
        return 4_000

    sealing_store = storage(database_path, clock_ms=sealing_clock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        sealing = executor.submit(
            sealing_store.interrupt_audio_asset,
            owner_id,
            seal_first.audio_asset_id,
        )
        assert sealing_has_lock.wait(timeout=5)
        accepting_after_seal = executor.submit(
            store.accept_audio_frame,
            owner_id,
            seal_first.audio_asset_id,
            sequence=0,
            start_ms=0,
            end_ms=1_000,
            media_type="audio/webm;codecs=opus",
            payload=b"frame",
        )
        release_seal.set()
        assert sealing.result().seal_reason == "interrupted"
        with pytest.raises(FrameConflict):
            accepting_after_seal.result()


def test_overlapping_frame_writers_store_and_charge_once(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Concurrent frames").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id

    def upload(payload: bytes) -> object:
        return storage(database_path).accept_audio_frame(
            owner_id,
            asset_id,
            sequence=0,
            start_ms=0,
            end_ms=1_000,
            media_type="audio/webm;codecs=opus",
            payload=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        identical = list(executor.map(upload, [b"same-frame", b"same-frame"]))

    assert [item.next_expected_sequence for item in identical] == [1, 1]
    detail = store.get_project(owner_id, project.id)
    assert detail.sections[0].audio_asset.frame_count == 1
    assert detail.sections[0].audio_asset.stored_bytes == 10

    competing_asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id

    def compete(payload: bytes) -> str:
        try:
            storage(database_path).accept_audio_frame(
                owner_id,
                competing_asset_id,
                sequence=0,
                start_ms=0,
                end_ms=1_000,
                media_type="audio/webm;codecs=opus",
                payload=payload,
            )
        except FrameConflict:
            return "conflict"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(compete, [b"first", b"second"]))

    assert sorted(outcomes) == ["accepted", "conflict"]
    competing = store.get_project(owner_id, project.id).sections[1].audio_asset
    assert competing.frame_count == 1
    assert competing.stored_bytes in {5, 6}


def test_usage_is_derived_from_owned_audio_assets(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    alice_id = user_id(database_path, "Alice")
    bob_id = user_id(database_path, "Bob")
    store = storage(database_path)
    alice_project = store.create_project(alice_id, uuid4(), "Alice").value
    bob_project = store.create_project(bob_id, uuid4(), "Bob").value
    alice_asset = store.create_section(
        alice_id,
        alice_project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    bob_asset = store.create_section(
        bob_id,
        bob_project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE audio_assets SET stored_bytes = 123 WHERE id = ?",
            (str(alice_asset),),
        )
        connection.execute(
            "UPDATE audio_assets SET stored_bytes = 999 WHERE id = ?",
            (str(bob_asset),),
        )

    assert store.used_audio_bytes(alice_id) == 123
    assert store.used_audio_bytes(bob_id) == 999


def test_database_constraints_guard_ordering_frames_and_seal_state(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Constraints").value
    section = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value
    assert accept_test_frame(database_path, section.audio_asset_id)

    with existing_connection(database_path, busy_timeout_ms=5_000) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE audio_assets SET seal_reason = 'completed' WHERE id = ?",
                (str(section.audio_asset_id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO sections (
                    id, project_id, position, creation_key, creation_media_type
                ) VALUES (?, ?, 0, ?, 'audio/webm;codecs=opus')
                """,
                (str(uuid4()), str(project.id), str(uuid4())),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO transport_frames (
                    audio_asset_id, sequence, start_ms, end_ms, media_type, sha256, payload
                ) VALUES (?, 0, 0, 1, 'audio/webm;codecs=opus', ?, X'01')
                """,
                (str(section.audio_asset_id), "1" * 64),
            )


def test_project_database_cascade_removes_stored_frames(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Delete later").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame",
    )

    with existing_connection(database_path, busy_timeout_ms=5_000) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM projects WHERE id = ?", (str(project.id),))
        connection.commit()
        frame_count = connection.execute("SELECT count(*) FROM transport_frames").fetchone()[0]

    assert frame_count == 0
    assert store.used_audio_bytes(owner_id) == 0
    with pytest.raises(ResourceNotFound):
        store.get_project(owner_id, project.id)


def test_audio_download_rejects_inconsistent_stored_byte_accounting(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Damaged accounting").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    store.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame",
    )
    store.complete_audio_asset(owner_id, asset_id, 1)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE audio_assets SET stored_bytes = 999 WHERE id = ?",
            (str(asset_id),),
        )

    with pytest.raises(AudioAssemblyFailure):
        store.stage_audio_download(owner_id, asset_id)


def test_audio_download_rejects_a_short_temporary_file_write(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    base = storage(database_path)
    project = base.create_project(owner_id, uuid4(), "Short write").value
    asset_id = base.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    base.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"frame",
    )
    base.complete_audio_asset(owner_id, asset_id, 1)

    staged_file_closed = Event()

    class ShortWritingFile:
        def __init__(self) -> None:
            self.wrapped = TemporaryFile(mode="w+b")  # noqa: SIM115 - storage owns it

        def write(self, payload: bytes) -> int:
            return self.wrapped.write(payload[:-1])

        def close(self) -> None:
            self.wrapped.close()
            staged_file_closed.set()

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

    downloader = storage(database_path, temporary_file_factory=ShortWritingFile)

    with pytest.raises(AudioAssemblyFailure):
        downloader.stage_audio_download(owner_id, asset_id)
    assert staged_file_closed.is_set()


def test_audio_assembly_failure_reports_hashes_near_the_sequence_gap(
    tmp_path: Path,
) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Sequence gap").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    payloads = [f"frame-{sequence}".encode() for sequence in range(5)]
    for sequence, payload in enumerate(payloads):
        store.accept_audio_frame(
            owner_id,
            asset_id,
            sequence=sequence,
            start_ms=sequence * 1_000,
            end_ms=(sequence + 1) * 1_000,
            media_type="audio/webm;codecs=opus",
            payload=payload,
        )
    store.complete_audio_asset(owner_id, asset_id, 5)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "DELETE FROM transport_frames WHERE audio_asset_id = ? AND sequence = 1",
            (str(asset_id),),
        )

    with pytest.raises(AudioAssemblyFailure) as raised:
        store.stage_audio_download(owner_id, asset_id)

    assert raised.value.offending_sequence == 2
    assert raised.value.nearby_hashes == (
        hashlib.sha256(payloads[0]).hexdigest(),
        hashlib.sha256(payloads[2]).hexdigest(),
    )


def test_audio_download_and_deletion_follow_snapshot_order(tmp_path: Path) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    base = storage(database_path)
    project = base.create_project(owner_id, uuid4(), "Deletion race").value
    asset_id = base.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    base.accept_audio_frame(
        owner_id,
        asset_id,
        sequence=0,
        start_ms=0,
        end_ms=1_000,
        media_type="audio/webm;codecs=opus",
        payload=b"retained snapshot",
    )
    base.complete_audio_asset(owner_id, asset_id, 1)
    snapshot_established = Event()
    allow_staging = Event()

    def paused_temporary_file():
        snapshot_established.set()
        assert allow_staging.wait(timeout=5)
        return TemporaryFile(mode="w+b")

    downloader = storage(database_path, temporary_file_factory=paused_temporary_file)
    with ThreadPoolExecutor(max_workers=1) as executor:
        staging = executor.submit(downloader.stage_audio_download, owner_id, asset_id)
        assert snapshot_established.wait(timeout=5)
        with existing_connection(database_path, busy_timeout_ms=5_000) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM projects WHERE id = ?", (str(project.id),))
            connection.commit()
        allow_staging.set()
        staged = staging.result(timeout=5)

    try:
        assert staged.stream.read() == b"retained snapshot"
    finally:
        staged.stream.close()
    with pytest.raises(ResourceNotFound):
        base.stage_audio_download(owner_id, asset_id)


def test_audio_download_returns_not_found_when_deletion_commits_first(
    tmp_path: Path,
) -> None:
    database_path = migrated_database(tmp_path)
    owner_id = user_id(database_path)
    store = storage(database_path)
    project = store.create_project(owner_id, uuid4(), "Deletion first").value
    asset_id = store.create_section(
        owner_id,
        project.id,
        uuid4(),
        "audio/webm;codecs=opus",
    ).value.audio_asset_id
    store.complete_audio_asset(owner_id, asset_id, 0)
    deletion_has_transaction = Event()
    download_is_waiting = Event()
    allow_deletion_commit = Event()
    deletion_committed = Event()

    def delete_project() -> None:
        with existing_connection(database_path, busy_timeout_ms=5_000) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM projects WHERE id = ?", (str(project.id),))
            deletion_has_transaction.set()
            assert allow_deletion_commit.wait(timeout=5)
            connection.commit()
        deletion_committed.set()

    def download_after_commit():
        assert deletion_has_transaction.wait(timeout=5)
        download_is_waiting.set()
        assert deletion_committed.wait(timeout=5)
        return store.stage_audio_download(owner_id, asset_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        deleting = executor.submit(delete_project)
        downloading = executor.submit(download_after_commit)
        assert download_is_waiting.wait(timeout=5)
        allow_deletion_commit.set()
        deleting.result(timeout=5)
        with pytest.raises(ResourceNotFound):
            downloading.result(timeout=5)
