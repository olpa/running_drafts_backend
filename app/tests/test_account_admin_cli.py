import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

from running_drafts.migrations import migrate
from running_drafts.settings import AppEnvironment, DatabaseSettings

ACCOUNT_ID = UUID("00000000-0000-4000-8000-000000000001")


def setup_database(tmp_path: Path) -> Path:
    path = tmp_path / "admin.sqlite3"
    migrate(DatabaseSettings(app_env=AppEnvironment.TEST, database_path=path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO users (id, created_at_ms) VALUES (?, 0)",
            (str(ACCOUNT_ID),),
        )
        connection.execute(
            """
            INSERT INTO provider_identities (
                issuer, subject, account_id, provider, email,
                first_login_at_ms, last_login_at_ms
            ) VALUES ('issuer', 'subject', ?, 'fixture', 'Person@Example.test', 0, 0)
            """,
            (str(ACCOUNT_ID),),
        )
    return path


def run(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    api_settings = {
        "DATABASE_MIN_FREE_BYTES",
        "DOWNLOAD_STAGING_DIRECTORY",
        "STAGING_MIN_FREE_BYTES",
        "MAX_TRANSPORT_FRAME_BYTES",
        "ACCOUNT_REQUESTS_PER_MINUTE",
        "ACCOUNT_REQUEST_BURST",
        "PROCESS_REQUESTS_PER_MINUTE",
        "PROCESS_REQUEST_BURST",
        "MAX_ACCOUNT_CONCURRENT_FRAME_UPLOADS",
        "MAX_PROCESS_CONCURRENT_FRAME_UPLOADS",
        "MAX_STAGED_DOWNLOAD_FILES",
    }
    environment = {name: value for name, value in os.environ.items() if name not in api_settings}
    return subprocess.run(
        [sys.executable, "-m", "running_drafts", *arguments],
        env=environment | {"APP_ENV": "test", "DATABASE_PATH": str(path)},
        text=True,
        capture_output=True,
        check=False,
    )


def test_lookup_grant_and_suspension_commands_need_no_api_settings(tmp_path: Path) -> None:
    path = setup_database(tmp_path)
    grant_id = uuid4()

    found = run(path, "account-find", "--email", "person@example.TEST")
    created = run(
        path,
        "allowance-grant-create",
        "--account-id",
        str(ACCOUNT_ID),
        "--grant-id",
        str(grant_id),
        "--stored-bytes",
        "100",
        "--reason",
        "support adjustment",
    )
    suspended = run(
        path,
        "account-suspend",
        "--account-id",
        str(ACCOUNT_ID),
        "--reason",
        "support request",
    )

    assert found.returncode == created.returncode == suspended.returncode == 0
    assert json.loads(found.stdout)["accountIds"] == [str(ACCOUNT_ID)]
    event = json.loads(created.stdout)
    assert event["actor"] == "local_admin"
    assert event["accountId"] == str(ACCOUNT_ID)
    assert event["after"]["id"] == str(grant_id)
    assert event["reason"] == "support adjustment"
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT stored_bytes FROM allowance_grants WHERE id = ?",
            (str(grant_id),),
        ).fetchone() == (100,)
        assert connection.execute(
            "SELECT suspended_at_ms IS NOT NULL FROM users WHERE id = ?",
            (str(ACCOUNT_ID),),
        ).fetchone() == (1,)
