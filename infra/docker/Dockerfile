# Multi-service image for CustomerRAG apps.
# Build: docker build --build-arg APP=api -f infra/docker/Dockerfile .
#
# APP values: api | s3_ingestion | chunk_worker
#             | embedding_sync_worker | ragas_eval_worker

ARG APP=api

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ARG APP
WORKDIR /app

# Copy workspace root configuration and lockfile
COPY pyproject.toml uv.lock ./

# Copy shared workspace packages required for dependency resolution
COPY packages/ ./packages/

# Copy target app configuration
COPY apps/${APP}/pyproject.toml ./apps/${APP}/

# Sync dependencies without installing the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    cd apps/${APP} && \
    uv sync --frozen --no-dev --no-install-project

# Copy the specific app source
COPY apps/${APP}/ ./apps/${APP}/

# Sync and install the specific app project
RUN --mount=type=cache,target=/root/.cache/uv \
    cd apps/${APP} && \
    uv sync --frozen --no-dev

FROM docker.io/library/python:3.12-slim-bookworm AS runtime

ARG APP
ENV APP=${APP} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1

WORKDIR /app

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

# Copy the built workspace (includes .venv, packages, and the specific app)
COPY --from=builder --chown=app:app /app /app

USER app

# Change runtime working directory to the specific app folder
WORKDIR /app/apps/${APP}

# API binds externally in Kubernetes; workers have no HTTP port.
HEALTHCHECK NONE

CMD ["python", "main.py"]
