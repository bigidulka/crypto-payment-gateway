# Production runtime deployment workflow

This document applies to the `arbitron-payment` Compose project after the limited database role has been prepared and approved. It is not a development quickstart and it does not authorize provider, payout, ledger lifecycle, reconciliation, or financial operations.

## Runtime services

Only these explicit services are valid runtime launch targets:

```text
api
worker-poller
worker-webhook
worker-sweeper
worker-expirer
```

Each service requires its own protected raw environment file outside the repository:

```text
/home/server/.config/arbitron-payment/runtime-api.env
/home/server/.config/arbitron-payment/runtime-worker-poller.env
/home/server/.config/arbitron-payment/runtime-worker-webhook.env
/home/server/.config/arbitron-payment/runtime-worker-sweeper.env
/home/server/.config/arbitron-payment/runtime-worker-expirer.env
```

The external directory must be mode `0700`; every runtime file must be a regular, non-symlink file, mode `0600`, owned by the approved deploy user. A runtime file contains the non-owner `DATABASE_URL`, Redis/application values, and no `POSTGRES_*`, nonempty `MIGRATION_DATABASE_URL`, other `MIGRATION_*`, `PGPASSWORD`, owner URL, or bootstrap value. The Compose override itself deliberately sets `MIGRATION_DATABASE_URL=""`; that empty key is the only migration-key exception. Do not copy a repository `.env` into a runtime file.

## Required image and Compose inputs

Every launch requires all of the following:

1. Base Compose file: `/home/server/Projects/arbitron-payment/docker-compose.yml`.
2. Limited-role override: `/home/server/Projects/arbitron-payment/docker-compose.runtime-role.yml`.
3. A protected external image override that pins the requested service to the reviewed immutable Docker image ID.
4. The explicit service-specific `RUNTIME_ENV_FILE` process variable.
5. Exact service name and `--no-build --no-deps`.

Use `scripts/runtime_compose.py` first. It is dry-run only: it verifies the approved service allowlist, protected runtime/env override paths, immutable image ID, raw env render equivalence, non-owner database user, empty migration URL, no owner/bootstrap names, inherited entrypoint, worker command or API `/ready` healthcheck. It never executes `docker compose up`.

## Canonical launch shape

After the planner succeeds and the change window explicitly authorizes the target service:

```text
RUNTIME_ENV_FILE=/home/server/.config/arbitron-payment/runtime-<service>.env \
docker compose -p arbitron-payment \
  -f /home/server/Projects/arbitron-payment/docker-compose.yml \
  -f /home/server/Projects/arbitron-payment/docker-compose.runtime-role.yml \
  -f <protected-immutable-image-override> \
  up -d --no-build --no-deps <service>
```

Never use bare production `docker compose up -d`, broad service groups, an unpinned tag, an implicit `.env`, or a runtime image to run Alembic.

## Migration owner

Schema migration is a separate approved one-shot operation using the explicit migration image and a temporary owner-only environment outside the repository. The migration credential must never enter API or worker runtime configuration. The existing owner role is not an application runtime role.

`0010_ledger_foundation` is additive. Its installation alone does not enable ledger posting, backfill, fee, provider, payout, or financial lifecycle behavior.

## Rollback

Before a runtime service switch, capture that target's exact prior image ID and its effective old environment into protected external rollback files. If startup/admission fails, restore only the affected service with its exact protected old image/config and `--no-build --no-deps`; stop further rollout. Do not automatically downgrade schema, rotate credentials, alter PUBLIC privileges, or switch unrelated services.

## Local development

The README development quickstart may use local `.env` and ordinary development commands. Those examples are not production deployment instructions.
