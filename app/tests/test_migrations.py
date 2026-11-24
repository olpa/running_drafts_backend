import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from running_drafts.database import configure_connection
from running_drafts.migrations import MIGRATIONS
from running_drafts.settings import AppEnvironment, DatabaseSettings

FIXTURES = Path(__file__).parent / "fixtures" / "mediarecorder" / "chromium-completed"


def run_command(command: str, database_path: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "APP_ENV": "test",
        "DATABASE_PATH": str(database_path),
    }
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def migrate_through(database_path: Path, version: int) -> None:
    settings = DatabaseSettings(app_env=AppEnvironment.TEST, database_path=database_path)
    with sqlite3.connect(database_path, isolation_level=None) as connection:
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        connection.execute("PRAGMA journal_mode = WAL")
        configure_connection(
            connection,
            busy_timeout_ms=settings.database_busy_timeout_ms,
            require_wal=True,
        )
        for migration in MIGRATIONS:
            if migration.version > version:
                break
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration.apply(connection)
                connection.execute(f"PRAGMA user_version = {migration.version}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def test_migrate_creates_the_database_at_the_schema_head(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"

    first = run_command("migrate", database_path)
    second = run_command("migrate", database_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["event"] == "database_migrated"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (2,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        child_columns = {
            table: {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("sections", "audio_assets", "transport_frames")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")
        }
    assert {
        "users",
        "projects",
        "sections",
        "audio_assets",
        "transport_frames",
        "provider_identities",
        "application_sessions",
        "oidc_login_attempts",
        "allowance_grants",
        "playback_clusters",
        "playback_packets",
    } <= tables
    assert "basic_auth_identities" not in tables
    assert "documents" not in tables
    assert "playback_clusters_range" in indexes
    assert all("owner_id" not in columns for columns in child_columns.values())
    assert {"created_at_ms", "updated_at_ms"} <= child_columns["audio_assets"]
    assert "sealed_at_ms" not in child_columns["audio_assets"]


def test_audio_asset_lifecycle_migration_preserves_dependent_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "version-six.sqlite3"
    migrate_through(database_path, 6)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO users (id, created_at_ms) VALUES ('account', 100)")
        connection.execute(
            """
            INSERT INTO projects (
                id, owner_id, creation_key, original_title, title,
                created_at_ms, updated_at_ms
            ) VALUES ('project', 'account', 'project-key', 'Title', 'Title', 100, 500)
            """
        )
        connection.executemany(
            """
            INSERT INTO sections (
                id, project_id, position, creation_key, creation_media_type
            ) VALUES (?, 'project', ?, ?, 'audio/webm;codecs=opus')
            """,
            (("open-section", 0, "open-key"), ("sealed-section", 1, "sealed-key")),
        )
        connection.execute(
            """
            INSERT INTO audio_assets (
                id, section_id, media_type, next_expected_sequence,
                observed_duration_ms, stored_bytes,
                playback_packet_count, playback_duration_samples,
                playback_cluster_count, playback_cluster_bytes
            ) VALUES (
                'open-asset', 'open-section', 'audio/webm;codecs=opus', 1, 1, 1,
                1, 1, 1, 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO audio_assets (
                id, section_id, media_type, is_sealed, seal_reason, sealed_at_ms
            ) VALUES (
                'sealed-asset', 'sealed-section', 'audio/webm;codecs=opus',
                1, 'completed', 300
            )
            """
        )
        connection.execute(
            """
            INSERT INTO transport_frames (
                audio_asset_id, sequence, start_ms, end_ms, media_type,
                sha256, payload, source_start, playback_etag
            ) VALUES (
                'open-asset', 0, 0, 1, 'audio/webm;codecs=opus',
                ?, X'01', 0, ?
            )
            """,
            ("0" * 64, "1" * 64),
        )
        connection.execute(
            """
            INSERT INTO playback_clusters (
                audio_asset_id, cluster_index, completed_sequence,
                first_packet_index, packet_count, start_samples,
                block_bytes, cumulative_bytes
            ) VALUES ('open-asset', 0, 1, 0, 1, 0, 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO playback_packets (
                audio_asset_id, packet_index, completed_sequence, cluster_index,
                source_offset, payload_size, duration_samples, start_samples,
                relative_ticks, block_offset
            ) VALUES ('open-asset', 0, 1, 0, 0, 1, 1, 0, 0, 0)
            """
        )

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute(
            """
            SELECT id, created_at_ms, updated_at_ms, is_sealed, seal_reason
            FROM audio_assets ORDER BY id
            """
        ).fetchall() == [
            ("open-asset", 100, 500, 0, None),
            ("sealed-asset", 100, 300, 1, "completed"),
        ]
        assert connection.execute(
            "SELECT payload FROM transport_frames WHERE audio_asset_id = 'open-asset'"
        ).fetchone() == (b"\x01",)
        assert connection.execute("SELECT count(*) FROM playback_clusters").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM playback_packets").fetchone() == (1,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audio_assets)")
        }
    assert {"created_at_ms", "updated_at_ms"} <= columns
    assert "sealed_at_ms" not in columns


def test_audio_asset_lifecycle_migration_rolls_back_invalid_chronology(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "invalid-version-six.sqlite3"
    migrate_through(database_path, 6)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO users (id, created_at_ms) VALUES ('account', 100)")
        connection.execute(
            """
            INSERT INTO projects (
                id, owner_id, creation_key, original_title, title,
                created_at_ms, updated_at_ms
            ) VALUES ('project', 'account', 'key', 'Title', 'Title', 100, 500)
            """
        )
        connection.execute(
            """
            INSERT INTO sections (
                id, project_id, position, creation_key, creation_media_type
            ) VALUES ('section', 'project', 0, 'key', 'audio/webm;codecs=opus')
            """
        )
        connection.execute(
            """
            INSERT INTO audio_assets (
                id, section_id, media_type, is_sealed, seal_reason, sealed_at_ms
            ) VALUES (
                'asset', 'section', 'audio/webm;codecs=opus', 1, 'completed', 99
            )
            """
        )

    result = run_command("migrate", database_path)

    assert result.returncode == 1
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(audio_assets)")
        }
        assert connection.execute(
            "SELECT sealed_at_ms FROM audio_assets WHERE id = 'asset'"
        ).fetchone() == (99,)
    assert "sealed_at_ms" in columns
    assert "created_at_ms" not in columns


def test_oidc_migration_advances_an_existing_schema_without_losing_accounts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.sqlite3"
    migrate_through(database_path, 2)
    account_id = "00000000-0000-4000-8000-000000000001"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (id, created_at_ms) VALUES (?, ?)",
            (account_id, 1),
        )

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute("SELECT id FROM users").fetchone() == (account_id,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert {"provider_identities", "application_sessions", "oidc_login_attempts"} <= tables
    assert "basic_auth_identities" not in tables


def test_basic_auth_removal_deletes_fixture_accounts_and_owned_data(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    migrate_through(database_path, 3)
    fixture_account = "00000000-0000-4000-8000-000000000001"
    retained_account = "00000000-0000-4000-8000-000000000002"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executemany(
            "INSERT INTO users (id, created_at_ms) VALUES (?, 0)",
            [(fixture_account,), (retained_account,)],
        )
        connection.execute(
            "INSERT INTO basic_auth_identities (username, user_id) VALUES ('fixture', ?)",
            (fixture_account,),
        )
        connection.execute(
            """
            INSERT INTO projects (
                id, owner_id, creation_key, original_title, title, created_at_ms, updated_at_ms
            ) VALUES ('00000000-0000-4000-8000-000000000003', ?, 'key', 'Old', 'Old', 0, 0)
            """,
            (fixture_account,),
        )

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT id FROM users").fetchall() == [(retained_account,)]
        assert connection.execute("SELECT count(*) FROM projects").fetchone() == (0,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert "basic_auth_identities" not in tables


def test_allowance_migration_backfills_free_plan_and_committed_bytes(tmp_path: Path) -> None:
    database_path = tmp_path / "version-four.sqlite3"
    migrate_through(database_path, 4)
    account_id = "00000000-0000-4000-8000-000000000001"
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO users (id, created_at_ms) VALUES (?, 0)", (account_id,))
        connection.execute(
            """
            INSERT INTO projects (
                id, owner_id, creation_key, original_title, title,
                created_at_ms, updated_at_ms
            ) VALUES ('project', ?, 'key', 'Title', 'Title', 0, 0)
            """,
            (account_id,),
        )
        connection.execute(
            """
            INSERT INTO sections (
                id, project_id, position, creation_key, creation_media_type
            ) VALUES ('section', 'project', 0, 'key', 'audio/webm;codecs=opus')
            """
        )
        connection.execute(
            """
            INSERT INTO audio_assets (id, section_id, media_type, stored_bytes)
            VALUES ('asset', 'section', 'audio/webm;codecs=opus', 7)
            """
        )

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT plan, stored_audio_bytes FROM users WHERE id = ?",
            (account_id,),
        ).fetchone() == ("free", 7)


def _version_five_database_with_frames(tmp_path: Path, frames: list[bytes]) -> Path:
    database_path = tmp_path / "version-five.sqlite3"
    migrate_through(database_path, 5)
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO users (id, created_at_ms) VALUES ('account', 0)")
        connection.execute(
            """
            INSERT INTO projects (
                id, owner_id, creation_key, original_title, title, created_at_ms, updated_at_ms
            ) VALUES ('project', 'account', 'key', 'Title', 'Title', 0, 0)
            """
        )
        connection.execute(
            """
            INSERT INTO sections (
                id, project_id, position, creation_key, creation_media_type
            ) VALUES ('section', 'project', 0, 'key', 'audio/webm;codecs=opus')
            """
        )
        connection.execute(
            """
            INSERT INTO audio_assets (
                id, section_id, media_type, next_expected_sequence,
                observed_duration_ms, stored_bytes
            ) VALUES ('asset', 'section', 'audio/webm;codecs=opus', ?, ?, ?)
            """,
            (len(frames), len(frames) * 1_000, sum(map(len, frames))),
        )
        for sequence, frame in enumerate(frames):
            connection.execute(
                """
                INSERT INTO transport_frames (
                    audio_asset_id, sequence, start_ms, end_ms,
                    media_type, sha256, payload
                ) VALUES ('asset', ?, ?, ?, 'audio/webm;codecs=opus', ?, ?)
                """,
                (
                    sequence,
                    sequence * 1_000,
                    (sequence + 1) * 1_000,
                    hashlib.sha256(frame).hexdigest(),
                    frame,
                ),
            )
    return database_path


def test_playback_migration_backfills_existing_browser_frames(tmp_path: Path) -> None:
    frames = [(FIXTURES / f"frame-{sequence:03}.webm").read_bytes() for sequence in range(3)]
    database_path = _version_five_database_with_frames(tmp_path, frames)

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        asset = connection.execute(
            """
            SELECT playback_packet_count, playback_duration_samples,
                   playback_cluster_count, playback_parse_offset
            FROM audio_assets WHERE id = 'asset'
            """
        ).fetchone()
        frame_rows = connection.execute(
            """
            SELECT source_start, playback_etag
            FROM transport_frames
            ORDER BY sequence
            """
        ).fetchall()
    assert asset[0] > 0
    assert asset[1] > 0
    assert asset[2] >= 3
    assert asset[3] <= sum(map(len, frames))
    assert [row[0] for row in frame_rows] == [0, len(frames[0]), len(frames[0]) + len(frames[1])]
    assert all(len(row[1]) == 64 for row in frame_rows)


def test_playback_migration_rolls_back_invalid_historical_media(tmp_path: Path) -> None:
    database_path = _version_five_database_with_frames(tmp_path, [b"not-webm"])

    result = run_command("migrate", database_path)

    assert result.returncode == 1
    assert "playback index backfill failed" in result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (5,)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(transport_frames)")
        }
    assert "playback_etag" not in columns
