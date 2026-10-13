import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence

import uvicorn
from pydantic import ValidationError

from running_drafts.api import create_app
from running_drafts.auth import ensure_authentication_adapter_is_available
from running_drafts.fixtures import FixtureError, create_fixtures
from running_drafts.migrations import MigrationError, migrate
from running_drafts.settings import DatabaseSettings, FixtureSettings, Settings
from running_drafts.structured_logging import LOGGING_CONFIG, write_event


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="running-drafts")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("fixtures")
    commands.add_parser("serve")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "migrate":
            version = migrate(DatabaseSettings())
            write_event("database_migrated", schema_version=version)
            return 0
        if arguments.command == "fixtures":
            inserted = create_fixtures(FixtureSettings())
            write_event("fixtures_created", insertedUsers=inserted)
            return 0
        if arguments.command == "serve":
            settings = Settings()
            ensure_authentication_adapter_is_available(settings)
            uvicorn.run(
                create_app(settings),
                host=settings.server_host,
                port=settings.server_port,
                workers=1,
                log_config=LOGGING_CONFIG,
            )
            return 0
    except (
        FixtureError,
        MigrationError,
        RuntimeError,
        ValidationError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(
            json.dumps(
                {"event": "command_failed", "error": str(error)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    raise AssertionError(f"unhandled command: {arguments.command}")


def main() -> None:
    raise SystemExit(run())
