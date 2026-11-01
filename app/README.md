# Running Drafts application backend

This directory is the FastAPI and SQLite application service. It is independent
from the Whisper service in `../inference`.

## Requirements

- Docker with Compose, or Python 3.15 and `uv`
- FFmpeg and FFprobe for the browser WebM/Opus evidence tests
- Explicit configuration; copy `.env.example` to `.env` before using Compose

`APP_ENV` must be `development`, `test`, or `production`. Protected APIs always
use browser Application Sessions. CORS origins are a JSON array and wildcards
are rejected.
The API has no defaults for capacity settings. The checked-in environment
samples explicitly configure the 2 MiB transport-frame bound, Account/process
request rates and bursts, Account/process upload concurrency, and the
two-file staged-download limit. The `db-admin` process does not require these
API-only settings.

`DATABASE_MIN_FREE_BYTES` applies to the filesystem containing
`DATABASE_PATH`. `STAGING_MIN_FREE_BYTES` applies independently to the
filesystem containing the mandatory absolute `DOWNLOAD_STAGING_DIRECTORY`.
The API performs a simple free-space preflight before accepting a new frame or
staging a known-size download. Starting below a minimum keeps the service ready
for reads and deletion. The staging directory must already exist and pass a
create/inspect/remove writability probe at API startup.

## Browser authentication

Browser authentication accepts `PUBLIC_EDITOR_URL`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, and optional `GOOGLE_REDIRECT_URI`. Supplying only one
Google credential is a startup error; supplying neither disables Google and
omits it from `GET /api/v1/auth/providers`. Development and production must use
separate Google registrations. Register these exact deployed callbacks:

- development: `https://drafts-api-dev.handsfree.vc/auth/callback/google`
- production: `https://drafts-api.handsfree.vc/auth/callback/google`

The backend uses Authorization Code flow with state, nonce, and PKCE S256.
Provider tokens are validated during the callback and never persisted. A
successful callback creates an opaque 14-day Application Session in the SQLite
database and sends only its random identifier as a host-only, HttpOnly,
SameSite=Lax cookie. The production cookie is Secure. Unsafe authenticated
requests require an exact configured `Origin` and browser API calls must include
credentials.

The browser contract is:

- `GET /api/v1/auth/providers`
- `GET /auth/login/{provider}?return_to=/projects`
- `GET /auth/callback/{provider}`
- `POST /auth/logout`
- `GET /api/v1/me`

For a local HTTP editor, set `SESSION_COOKIE_SECURE=false`; production rejects
that setting. Run a manual live-provider smoke only against a deployed target:
open a fresh private browser, start Google login, complete consent, confirm the
browser returns to the requested editor path, verify `/api/v1/me` reports the
Account and authoritative expiry, create private content, then log out and
confirm protected requests return `401 authentication_required`. Repeat login
and confirm it resolves the same Account. Do not record tokens, cookies, codes,
claims, credentials, or provider response bodies as evidence.

## Online fixture control

The optional test-control API is available in any environment only when both
`TEST_CONTROL_BOOTSTRAP_CREDENTIAL` and `TEST_CONTROL_DERIVATION_KEY` are
nonempty. With either absent, its routes are not registered. Enabling it in a
deployed environment is an explicit operator decision.

A trusted provisioning client authenticates to
`GET /api/v1/test-control/challenge` with the bootstrap Bearer credential. It
validates `hmac-sha256` and context `running-drafts:test-control:v1:`, then
derives an instance token from the UTF-8 derivation key and decoded challenge.
It sends that token as `X-Test-Control` to synchronous
`POST /api/v1/test-control/reset` and `POST /api/v1/test-control/create` calls.
The challenge and derived token change after every API-process restart.

Reset transactionally deletes only the fixed fixture Account and its cascaded
state. Create restores its canonical identity and sets the normal
`running_drafts_session` HttpOnly cookie. Neither endpoint is an alternate
authentication mechanism for application resources. The product sandbox
manager owns the exchange for local E2E sandboxes; pytest reads only its
protected ready-to-send headers file. The product integration sequence and
consumer responsibilities are recorded in the
[test-control E2E handover](../docs/test-control-e2e-handover.md).

## Docker Compose workflow

Database migration and API startup are intentionally separate:

```bash
cp .env.example .env
docker compose run --rm --build db-admin migrate
docker compose up --build api
```

The first command builds the current `db-admin` image before applying migrations;
this avoids reusing an image produced from older source or lockfile contents.

Neither `docker compose up` nor API startup creates, migrates, or seeds the
database. Compose stores SQLite in the named `application-data` volume and
stages downloads in the separate `download-staging` volume. The API
container remains alive when the database is unavailable or at the wrong schema
version, but its health check remains unhealthy because `/health/ready` returns
503.

## Local workflow

Export the values shown in `.env.example`, set `DATABASE_PATH` to a named file,
and run the same explicit sequence:

```bash
uv sync --frozen
uv run running-drafts migrate
uv run running-drafts serve
```

The API listens on port 8000 by default. Development and test expose `/docs`
and `/openapi.json`; production disables both. `/health/live` never touches the
database. `/health/ready` opens the database without creating it, runs a query,
checks WAL mode, and requires the exact application migration head.

## Plans, allowances, and local administration

Every Account starts on the source-controlled Free Plan. The API reports the
effective Plan, aggregate active grants, usage, warning threshold, and hard
limit through `GET /api/v1/me/usage`. A newly accepted frame and its Account
storage counter commit in one SQLite transaction. Exact frame retries remain
uncharged and successful at a hard boundary.

The one-shot `db-admin` container exposes `account-find`, operator Allowance
Grant create/patch/remove, Downgrade Grant patch/remove, and Account
suspend/reinstate commands. Mutations require an explicit Account UUID and
reason and emit a structured success event. Suspension deletes existing
Application Sessions without deleting Account data.

## Database space maintenance

New databases use SQLite incremental auto-vacuum. Deleted pages remain reusable
immediately; returning them to the host filesystem is an explicit bounded
maintenance operation:

```bash
uv run running-drafts incremental-vacuum --pages 1000
```

Run small batches periodically or after substantial deletion. Each batch is a
write operation and may briefly wait for or delay another writer. In WAL mode,
the physical database file is truncated when the vacuum changes are checkpointed.

## Tests

```bash
uv run pytest
```

The suite uses named temporary SQLite files and the real connection, migration,
test-control, authentication, and readiness paths. Container smoke verification also
requires access to the Docker daemon.

Run the opt-in image build, named-volume, migration, test-control, health, and restart
smoke test with:

```bash
RUN_CONTAINER_TESTS=1 uv run pytest -m container
```

## Host deployment

The host deployment uses a locally built, explicitly tagged image and separate
development and production SQLite directories under
`/rnotes/RUNNING_DRAFTS_BACKEND`. Build a release from this directory with:

```bash
IMAGE_TAG=0.1.0 make image
```

The deployment Compose file publishes plain HTTP on loopback only. boringproxy
owns public HTTPS and its certificates. See [`deployment/README.md`](deployment/README.md)
for target names, ports, migration, startup, backup, and rollback commands.

The real-browser ordered WebM assembly evidence and deterministic empty-audio
fixture are documented in [`../docs/webm-assembly-evidence.md`](../docs/webm-assembly-evidence.md).
