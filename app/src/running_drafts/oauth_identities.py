from dataclasses import dataclass
from uuid import UUID, uuid4

from running_drafts import clock
from running_drafts.database import existing_connection
from running_drafts.oauth import ProviderClaims
from running_drafts.settings import Settings


@dataclass(frozen=True)
class AccountIdentity:
    account_id: UUID
    claims: ProviderClaims


def resolve_or_provision_identity(
    settings: Settings,
    claims: ProviderClaims,
    *,
    now_ms: int | None = None,
) -> AccountIdentity:
    login_at_ms = clock.utc_milliseconds() if now_ms is None else now_ms
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                """
                SELECT account_id
                FROM provider_identities
                WHERE issuer = ? AND subject = ?
                """,
                (claims.issuer, claims.subject),
            ).fetchone()
            if row is None:
                account_id = uuid4()
                connection.execute(
                    "INSERT INTO users (id, created_at_ms) VALUES (?, ?)",
                    (str(account_id), login_at_ms),
                )
                connection.execute(
                    """
                    INSERT INTO provider_identities (
                        issuer, subject, account_id, provider, email, email_verified,
                        display_name, given_name, family_name, picture_url, locale,
                        microsoft_tenant_id, first_login_at_ms, last_login_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claims.issuer,
                        claims.subject,
                        str(account_id),
                        claims.provider,
                        claims.email,
                        claims.email_verified,
                        claims.display_name,
                        claims.given_name,
                        claims.family_name,
                        claims.picture_url,
                        claims.locale,
                        claims.microsoft_tenant_id,
                        login_at_ms,
                        login_at_ms,
                    ),
                )
            else:
                account_id = UUID(row[0])
                connection.execute(
                    """
                    UPDATE provider_identities
                    SET email = COALESCE(?, email),
                        email_verified = COALESCE(?, email_verified),
                        display_name = COALESCE(?, display_name),
                        given_name = COALESCE(?, given_name),
                        family_name = COALESCE(?, family_name),
                        picture_url = COALESCE(?, picture_url),
                        locale = COALESCE(?, locale),
                        microsoft_tenant_id = COALESCE(?, microsoft_tenant_id),
                        last_login_at_ms = MAX(last_login_at_ms, ?)
                    WHERE issuer = ? AND subject = ?
                    """,
                    (
                        claims.email,
                        claims.email_verified,
                        claims.display_name,
                        claims.given_name,
                        claims.family_name,
                        claims.picture_url,
                        claims.locale,
                        claims.microsoft_tenant_id,
                        login_at_ms,
                        claims.issuer,
                        claims.subject,
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return get_account_identity(settings, account_id)


def get_account_identity(settings: Settings, account_id: UUID) -> AccountIdentity:
    with existing_connection(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
    ) as connection:
        row = connection.execute(
            """
            SELECT provider, issuer, subject, email, email_verified, display_name,
                   given_name, family_name, picture_url, locale, microsoft_tenant_id
            FROM provider_identities
            WHERE account_id = ?
            """,
            (str(account_id),),
        ).fetchone()
    if row is None:
        raise LookupError("account identity does not exist")
    return AccountIdentity(
        account_id=account_id,
        claims=ProviderClaims(
            provider=row[0],
            issuer=row[1],
            subject=row[2],
            email=row[3],
            email_verified=None if row[4] is None else bool(row[4]),
            display_name=row[5],
            given_name=row[6],
            family_name=row[7],
            picture_url=row[8],
            locale=row[9],
            microsoft_tenant_id=row[10],
        ),
    )
