import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from running_drafts.database import existing_connection
from running_drafts.project_storage import ProjectStorage


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
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepting = executor.submit(
            accept_test_frame,
            database_path,
            frame_first.audio_asset_id,
            ready_to_commit=frame_ready,
            allow_commit=allow_frame_commit,
        )
        assert frame_ready.wait(timeout=5)
        sealing_after_frame = executor.submit(
            store.complete_audio_asset,
            owner_id,
            frame_first.audio_asset_id,
            1,
        )
        allow_frame_commit.set()
        assert accepting.result()
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
            accept_test_frame,
            database_path,
            seal_first.audio_asset_id,
        )
        release_seal.set()
        assert sealing.result().seal_reason == "interrupted"
        assert not accepting_after_seal.result()


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
