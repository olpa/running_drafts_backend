# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv

FROM python:3.15.0-slim AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.15.0-slim AS runtime
RUN groupadd --system running-drafts \
    && useradd --system --gid running-drafts --home-dir /app running-drafts \
    && mkdir /data \
    && chown running-drafts:running-drafts /data
WORKDIR /app
COPY --from=builder --chown=running-drafts:running-drafts /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER running-drafts

FROM runtime AS api
EXPOSE 8000
ENTRYPOINT ["running-drafts"]
CMD ["serve"]

FROM runtime AS db-admin
ENTRYPOINT ["running-drafts"]
