import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"


@dataclass(frozen=True)
class PlanPolicy:
    storage_warning_bytes: int
    storage_limit_bytes: int
    duration_warning_ms: int
    duration_limit_ms: int


PLAN_POLICIES = {
    Plan.FREE: PlanPolicy(
        storage_warning_bytes=107_374_182,
        storage_limit_bytes=134_217_728,
        duration_warning_ms=2_880_000,
        duration_limit_ms=3_600_000,
    ),
    Plan.PRO: PlanPolicy(
        storage_warning_bytes=858_993_459,
        storage_limit_bytes=1_073_741_824,
        duration_warning_ms=11_520_000,
        duration_limit_ms=14_400_000,
    ),
}


@dataclass(frozen=True)
class EffectiveAllowance:
    plan: Plan
    used_storage_bytes: int
    granted_storage_bytes: int
    storage_warning_bytes: int
    storage_limit_bytes: int
    granted_duration_ms: int
    duration_warning_ms: int
    duration_limit_ms: int


def effective_allowance(
    connection: sqlite3.Connection,
    account_id: str,
) -> EffectiveAllowance:
    account = connection.execute(
        "SELECT plan, stored_audio_bytes FROM users WHERE id = ?",
        (account_id,),
    ).fetchone()
    if account is None:
        raise LookupError("account does not exist")
    plan = Plan(account["plan"] if isinstance(account, sqlite3.Row) else account[0])
    used = account["stored_audio_bytes"] if isinstance(account, sqlite3.Row) else account[1]
    operator = connection.execute(
        """
        SELECT COALESCE(SUM(stored_bytes), 0), COALESCE(SUM(duration_ms), 0)
        FROM allowance_grants
        WHERE account_id = ? AND kind = 'operator'
        """,
        (account_id,),
    ).fetchone()
    granted_storage = operator[0]
    granted_duration = operator[1]
    if plan is Plan.FREE:
        downgrade = connection.execute(
            """
            SELECT COALESCE(SUM(stored_bytes), 0)
            FROM allowance_grants
            WHERE account_id = ? AND kind = 'downgrade'
            """,
            (account_id,),
        ).fetchone()[0]
        granted_storage += downgrade
    policy = PLAN_POLICIES[plan]
    return EffectiveAllowance(
        plan=plan,
        used_storage_bytes=used,
        granted_storage_bytes=granted_storage,
        storage_warning_bytes=policy.storage_warning_bytes + granted_storage,
        storage_limit_bytes=policy.storage_limit_bytes + granted_storage,
        granted_duration_ms=granted_duration,
        duration_warning_ms=policy.duration_warning_ms + granted_duration,
        duration_limit_ms=policy.duration_limit_ms + granted_duration,
    )
