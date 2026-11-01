import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from uuid import UUID, uuid4

import uvicorn
from pydantic import ValidationError

from running_drafts.account_admin import (
    AccountAdministration,
    AccountNotFound,
    GrantIdConflict,
)
from running_drafts.api import create_app
from running_drafts.maintenance import MaintenanceError, incremental_vacuum
from running_drafts.migrations import MigrationError, migrate
from running_drafts.settings import DatabaseSettings, Settings
from running_drafts.structured_logging import LOGGING_CONFIG, write_event


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="running-drafts")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    vacuum = commands.add_parser("incremental-vacuum")
    vacuum.add_argument("--pages", type=int, default=1_000)
    commands.add_parser("serve")
    find = commands.add_parser("account-find")
    find.add_argument("--email", required=True)
    for name in ("allowance-grant-create", "allowance-grant-patch"):
        grant = commands.add_parser(name)
        grant.add_argument("--account-id", type=UUID, required=True)
        grant.add_argument("--grant-id", type=UUID, required=True)
        grant.add_argument("--stored-bytes", type=_nonnegative_int)
        grant.add_argument("--duration-ms", type=_nonnegative_int)
        grant.add_argument("--reason", required=True)
    remove = commands.add_parser("allowance-grant-remove")
    remove.add_argument("--account-id", type=UUID, required=True)
    remove.add_argument("--grant-id", type=UUID, required=True)
    remove.add_argument("--reason", required=True)
    downgrade_patch = commands.add_parser("downgrade-grant-patch")
    downgrade_patch.add_argument("--account-id", type=UUID, required=True)
    downgrade_patch.add_argument("--stored-bytes", type=_nonnegative_int, required=True)
    downgrade_patch.add_argument("--reason", required=True)
    downgrade_remove = commands.add_parser("downgrade-grant-remove")
    downgrade_remove.add_argument("--account-id", type=UUID, required=True)
    downgrade_remove.add_argument("--reason", required=True)
    for name in ("account-suspend", "account-reinstate"):
        account = commands.add_parser(name)
        account.add_argument("--account-id", type=UUID, required=True)
        account.add_argument("--reason", required=True)
    return parser


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {name: _json_value(item) for name, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(name): _json_value(item) for name, item in value.items()}
    return value


def _admin(settings: DatabaseSettings) -> AccountAdministration:
    return AccountAdministration(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    )


def _write_admin_result(operation: str, account_id: UUID, reason: str, result: object) -> None:
    before = result.before
    after = result.after
    write_event(
        "account_administration_succeeded",
        actor="local_admin",
        operation=operation,
        eventId=str(uuid4()),
        accountId=str(account_id),
        before=_json_value(before),
        after=_json_value(after),
        reason=reason,
    )


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "migrate":
            version = migrate(DatabaseSettings())
            write_event("database_migrated", schema_version=version)
            return 0
        if arguments.command == "incremental-vacuum":
            result = incremental_vacuum(DatabaseSettings(), pages=arguments.pages)
            write_event(
                "database_incrementally_vacuumed",
                pagesBefore=result.pages_before,
                pagesAfter=result.pages_after,
                reclaimedPages=result.reclaimed_pages,
            )
            return 0
        if arguments.command == "serve":
            settings = Settings()
            uvicorn.run(
                create_app(settings),
                host=settings.server_host,
                port=settings.server_port,
                workers=1,
                log_config=LOGGING_CONFIG,
            )
            return 0
        settings = DatabaseSettings()
        administration = _admin(settings)
        if arguments.command == "account-find":
            write_event(
                "accounts_found",
                email=arguments.email,
                accountIds=[str(value) for value in administration.find_by_email(arguments.email)],
            )
            return 0
        if arguments.command == "allowance-grant-create":
            result = administration.create_grant(
                arguments.account_id,
                arguments.grant_id,
                stored_bytes=arguments.stored_bytes or 0,
                duration_ms=arguments.duration_ms or 0,
            )
        elif arguments.command == "allowance-grant-patch":
            result = administration.patch_grant(
                arguments.account_id,
                arguments.grant_id,
                stored_bytes=arguments.stored_bytes,
                duration_ms=arguments.duration_ms,
            )
        elif arguments.command == "allowance-grant-remove":
            result = administration.remove_grant(arguments.account_id, arguments.grant_id)
        elif arguments.command == "downgrade-grant-patch":
            result = administration.patch_downgrade_grant(
                arguments.account_id,
                stored_bytes=arguments.stored_bytes,
            )
        elif arguments.command == "downgrade-grant-remove":
            result = administration.remove_downgrade_grant(arguments.account_id)
        elif arguments.command == "account-suspend":
            result = administration.suspend(arguments.account_id)
        elif arguments.command == "account-reinstate":
            result = administration.reinstate(arguments.account_id)
        else:
            raise AssertionError(f"unhandled command: {arguments.command}")
        _write_admin_result(
            arguments.command,
            arguments.account_id,
            arguments.reason,
            result,
        )
        return 0
    except (
        MaintenanceError,
        MigrationError,
        AccountNotFound,
        GrantIdConflict,
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
