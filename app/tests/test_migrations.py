import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

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


def remove_allowance_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE allowance_grants")
    connection.execute("ALTER TABLE users DROP COLUMN suspended_at_ms")
    connection.execute("ALTER TABLE users DROP COLUMN stored_audio_bytes")
    connection.execute("ALTER TABLE users DROP COLUMN plan")


def remove_playback_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE playback_packets")
    connection.execute("DROP INDEX playback_clusters_range")
    connection.execute("DROP TABLE playback_clusters")
    connection.execute("DROP INDEX transport_frames_playback_source")
    connection.execute("ALTER TABLE transport_frames DROP COLUMN source_start")
    connection.execute("ALTER TABLE transport_frames DROP COLUMN playback_etag")
    for column in (
        "playback_parse_offset",
        "playback_parser_phase",
        "playback_cluster_end",
        "playback_packet_count",
        "playback_duration_samples",
        "playback_cluster_count",
        "playback_cluster_bytes",
        "playback_source_track_number",
        "playback_codec_private",
        "playback_sample_rate",
        "playback_channels",
        "playback_codec_delay_ns",
        "playback_seek_pre_roll_ns",
    ):
        connection.execute(f"ALTER TABLE audio_assets DROP COLUMN {column}")


def test_migrate_creates_the_database_at_the_schema_head(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"

    first = run_command("migrate", database_path)
    second = run_command("migrate", database_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["event"] == "database_migrated"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
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


def test_oidc_migration_advances_an_existing_schema_without_losing_accounts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    account_id = "00000000-0000-4000-8000-000000000001"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO users (id, created_at_ms) VALUES (?, ?)",
            (account_id, 1),
        )
        connection.execute("DROP TABLE oidc_login_attempts")
        connection.execute("DROP TABLE application_sessions")
        connection.execute("DROP TABLE provider_identities")
        remove_playback_schema(connection)
        remove_allowance_schema(connection)
        connection.execute(
            """
            CREATE TABLE basic_auth_identities (
                username TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("PRAGMA user_version = 2")

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        assert connection.execute("SELECT id FROM users").fetchone() == (account_id,)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
    assert {"provider_identities", "application_sessions", "oidc_login_attempts"} <= tables
    assert "basic_auth_identities" not in tables


def test_basic_auth_removal_deletes_fixture_accounts_and_owned_data(tmp_path: Path) -> None:
    database_path = tmp_path / "application.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    fixture_account = "00000000-0000-4000-8000-000000000001"
    retained_account = "00000000-0000-4000-8000-000000000002"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        remove_playback_schema(connection)
        remove_allowance_schema(connection)
        connection.execute(
            """
            CREATE TABLE basic_auth_identities (
                username TEXT PRIMARY KEY NOT NULL COLLATE BINARY,
                user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
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
        connection.execute("PRAGMA user_version = 3")

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
    assert run_command("migrate", database_path).returncode == 0
    account_id = "00000000-0000-4000-8000-000000000001"
    with sqlite3.connect(database_path) as connection:
        remove_playback_schema(connection)
        remove_allowance_schema(connection)
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
        connection.execute("PRAGMA user_version = 4")

    result = run_command("migrate", database_path)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT plan, stored_audio_bytes FROM users WHERE id = ?",
            (account_id,),
        ).fetchone() == ("free", 7)


def _version_five_database_with_frames(tmp_path: Path, frames: list[bytes]) -> Path:
    database_path = tmp_path / "version-five.sqlite3"
    assert run_command("migrate", database_path).returncode == 0
    with sqlite3.connect(database_path) as connection:
        remove_playback_schema(connection)
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
        connection.execute("PRAGMA user_version = 5")
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
