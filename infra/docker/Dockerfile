# Multi-service image for CustomerRAG apps.
# Build: docker build --build-arg APP=api -f infra/docker/Dockerfile .
#
# APP values: api | s3_ingestion | local_chunk_worker | external_chunk_worker
#             | embedding_sync_worker | ragas_eval_worker

ARG APP=api

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ARG APP
WORKDIR /build

COPY apps/${APP}/pyproject.toml apps/${APP}/uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
      uv sync --frozen --no-dev --no-install-project; \
    else \
      uv sync --no-dev --no-install-project; \
    fi

COPY apps/${APP}/ ./

RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -f uv.lock ]; then \
      uv sync --frozen --no-dev; \
    else \
      uv sync --no-dev; \
    fi

FROM python:3.13-slim-bookworm AS runtime

ARG APP
ENV APP=${APP} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1

WORKDIR /app

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder --chown=app:app /build /app

USER app

# API binds externally in Kubernetes; workers have no HTTP port.
HEALTHCHECK NONE

CMD ["python", "main.py"]
