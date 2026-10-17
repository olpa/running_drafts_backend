# Running Drafts application backend

This directory is the FastAPI and SQLite application service. It is independent
from the Whisper service in `../inference`.

## Requirements

- Docker with Compose, or Python 3.15 and `uv`
- FFmpeg and FFprobe for the browser WebM/Opus evidence tests
- Explicit configuration; copy `.env.example` to `.env` and replace the
  development password before using Compose

`APP_ENV` must be `development`, `test`, or `production`. `AUTH_MODE=basic` is
available only in development and test. `AUTH_MODE=auth0` deliberately fails at
startup until backend issue #8 supplies the production adapter. CORS origins and
Basic usernames are JSON arrays. CORS wildcards are rejected.
`MAX_TRANSPORT_FRAME_BYTES` bounds each streamed audio frame and defaults to
2097152 bytes (2 MiB).
`MAX_STAGED_DOWNLOAD_FILES` bounds concurrent audio downloads per application
process and defaults to 20.

## Docker Compose workflow

Database creation, fixtures, and API startup are intentionally separate:

```bash
cp .env.example .env
docker compose run --rm --build db-admin migrate
docker compose run --rm db-admin fixtures
docker compose up --build api
```

The first command builds the current `db-admin` image before applying migrations;
this avoids reusing an image produced from older source or lockfile contents.

Neither `docker compose up` nor API startup creates, migrates, or seeds the
database. Compose stores SQLite in the named `application-data` volume. The API
container remains alive when the database is unavailable or at the wrong schema
version, but its health check remains unhealthy because `/health/ready` returns
503.

## Local workflow

Export the values shown in `.env.example`, set `DATABASE_PATH` to a named file,
and run the same explicit sequence:

```bash
uv sync --frozen
uv run running-drafts migrate
uv run running-drafts fixtures
uv run running-drafts serve
```

The API listens on port 8000 by default. Development and test expose `/docs`
and `/openapi.json`; production disables both. `/health/live` never touches the
database. `/health/ready` opens the database without creating it, runs a query,
checks WAL mode, and requires the exact application migration head.

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
fixture, authentication, and readiness paths. Container smoke verification also
requires access to the Docker daemon.

Run the opt-in image build, named-volume, migration, fixture, health, and restart
smoke test with:

```bash
RUN_CONTAINER_TESTS=1 uv run pytest -m container
```

The real-browser ordered WebM assembly evidence and deterministic empty-audio
fixture are documented in [`../docs/webm-assembly-evidence.md`](../docs/webm-assembly-evidence.md).
