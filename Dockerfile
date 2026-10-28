# syntax=docker/dockerfile:1

FROM python:3.12.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appgroup && \
    useradd --uid 1000 --gid appgroup --shell /bin/bash --create-home appuser

FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir=/app/wheels -r requirements.txt

# Migration-owner image: invoked externally with only MIGRATION_DATABASE_URL and
# owner credentials supplied by the approved secret-delivery mechanism. It is
# never used by API/worker runtime containers.
FROM base AS migration

COPY --from=builder /app/wheels /wheels
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir /wheels/* && \
    rm -rf /wheels

COPY --chown=appuser:appgroup src ./src
COPY --chown=appuser:appgroup config/chains.toml ./config/chains.toml
COPY --chown=appuser:appgroup config/providers.toml ./config/providers.toml
COPY --chown=appuser:appgroup alembic ./alembic
COPY --chown=appuser:appgroup alembic.ini ./alembic.ini

USER appuser
CMD ["alembic", "upgrade", "head"]

# Application runtime image is deliberately the final/default target. Explicit
# allowlist only: operator env, migrations, scripts, tests, dumps and compose
# artifacts are absent.
FROM base AS production

COPY --from=builder /app/wheels /wheels
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir /wheels/* && \
    rm -rf /wheels

COPY --chown=appuser:appgroup src ./src
COPY --chown=appuser:appgroup config/chains.toml ./config/chains.toml
COPY --chown=appuser:appgroup config/providers.toml ./config/providers.toml
COPY --chown=appuser:appgroup docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

USER appuser
ENTRYPOINT ["/app/docker-entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000
