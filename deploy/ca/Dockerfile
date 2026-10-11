# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app
FROM base AS wheels
COPY deploy/ca/gateway.runtime.lock /build/runtime.lock
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir=/wheels -r /build/runtime.lock
FROM base AS runtime
ARG BUILD_COMMIT
LABEL org.opencontainers.image.revision=$BUILD_COMMIT
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --no-deps /wheels/* && rm -rf /wheels \
    && mkdir -p /data && chown app:app /data
COPY --chown=app:app src /app/src
COPY --chown=app:app config/chains.toml /app/config/chains.toml
COPY --chown=app:app requirements.txt /app/requirements.txt
COPY --chown=app:app docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
USER app
ENTRYPOINT ["/app/docker-entrypoint.sh"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD curl -fsS http://localhost:8000/health >/dev/null || exit 1
