# Running Drafts backend deployment

This directory supplies the host scripts installed at
`/rnotes/RUNNING_DRAFTS_BACKEND`. The application image and deployment Compose
file remain in `/mnt/HC_Volume_103849597/home/olpa/running_drafts_backend/app`.
The source repository is `olpa/running_drafts_backend`; the host operator owns
this deployment.

Build a tagged image from `app/`:

```sh
IMAGE_TAG=0.1.0 make image
```

Each target selects its deployed tag only through `.env.dev` or `.env.prod`.
For a normal rollout, build the tag named in the target env file, back up an
existing database, migrate explicitly, and start the target:

```sh
/rnotes/RUNNING_DRAFTS_BACKEND/backup-dev.sh
/rnotes/RUNNING_DRAFTS_BACKEND/migrate-dev.sh
/rnotes/RUNNING_DRAFTS_BACKEND/start-dev.sh
```

Omit the backup only for the first migration of a new, empty target. Never run
the `fixtures` command for these deployments.

Development uses Compose project `running-drafts-backend-dev`, loopback HTTP
port 18081, and `data-dev`. Production uses
`running-drafts-backend-prod`, loopback HTTP port 18082, and `data-prod`.
boringproxy terminates public HTTPS for `drafts-api-dev.handsfree.vc` and
`drafts-api.handsfree.vc` and proxies plain HTTP to those listeners. Its route
database owns certificate issuance and renewal; this deployment owns no TLS
private keys. The boringproxy routes use server TLS termination and target
ports 18081 and 18082 respectively.

The health endpoints are public. Browser authentication uses direct Google OIDC
and server-side Application Sessions. Development and production use separate
Google registrations and these exact callbacks:

- `https://drafts-api-dev.handsfree.vc/auth/callback/google`
- `https://drafts-api.handsfree.vc/auth/callback/google`

Keep client secrets only in the target `.env.dev` and `.env.prod` files on the
host; never copy them into the repository or logs. `/health/live` reports
process health and `/health/ready` additionally requires the explicitly
migrated SQLite database.

Use `status-dev.sh` or `status-prod.sh` for local readiness, and pass normal
Compose log options to `logs-dev.sh` or `logs-prod.sh`. `backup-dev.sh` and
`backup-prod.sh` use SQLite's online backup API and write outside the live data
directory. Retention is currently manual; do not remove the last known-good
backup.

Restore is deliberately manual because it replaces durable data. Stop only the
selected Compose project, preserve the live database together with any WAL/SHM
files, verify the selected backup with `PRAGMA integrity_check`, copy it to
`data-dev/running-drafts.sqlite3` or `data-prod/running-drafts.sqlite3`, remove
stale WAL/SHM files only after they have been preserved, restore ownership to
numeric UID/GID `1000:1000`, then start the target and check readiness. Never
restore one environment's backup into the other environment.

To roll back, set the prior immutable `IMAGE_TAG` in the target env file and
run its start script. Application rollback is safe only when the prior image
supports the current database migration head.

The expected checks are:

```sh
/rnotes/RUNNING_DRAFTS_BACKEND/status-dev.sh
curl --fail https://drafts-api-dev.handsfree.vc/health/live
curl --fail https://drafts-api-dev.handsfree.vc/health/ready
```

Use the corresponding `prod` script and hostname for production.

For the deployed Google smoke, use a fresh private browser, sign in through the
target editor, verify the requested editor path is restored and `/api/v1/me`
returns an Account plus `sessionExpiresAt`, create and retrieve a private draft,
log out, and verify protected access returns `401 authentication_required`.
Sign in again and verify the same Account and draft return. Record only target,
timestamp, provider, result, and any safe correlation ID—never credentials,
authorization codes, tokens, cookies, subjects, emails, or raw claims.
