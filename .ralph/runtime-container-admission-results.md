# Runtime container admission results

Date: 2026-09-05
Status: LOCAL-only candidate image/admission rehearsal. No production Docker host, image, source, environment, role, credential, migration, restart, deployment, push, provider, or financial action was used.

## Build transport diagnosis

The bounded probes from the exact `python:3.12-slim` image behaved differently by Docker network mode:

```text
bridge: official Debian HTTP TCP timed out; official Debian HTTPS timed out
host Python: deb.debian.org HTTPS=200; pypi.org/simple HTTPS=200
base image with --network=host: Debian HTTPS=200; PyPI HTTPS=200
```

No Docker daemon/firewall/global proxy/mirror/TLS/signature setting was changed. The exact Dockerfile built locally using only:

```text
docker build --network=host -t arbitron-runtime-role-candidate:local .
```

Runtime image:

```text
sha256:750676dbe2dcbe364615eec7067c8a2953c78b4c3150966b767c3f34e4f32f3e
entrypoint=["/app/docker-entrypoint.sh"]
default cmd=null
```

Explicit migration target built separately:

```text
sha256:b9296d50691ab40df2977ef86fd8e1166e97ceb675c7f56173458a0495e3b395
cmd=["alembic","upgrade","head"]
```

Default Dockerfile build is runtime; migration requires explicit `--target migration`.

## Runtime image context proof

A dummy ignored local marker was placed at `secrets/OWNER_SECRET_CANARY.env`, then the exact image was rebuilt. Runtime image path and image-layer-name inspection confirmed absence of:

```text
/app/.env
/app/.env.example
/app/alembic.ini
/app/alembic/
/app/secrets/OWNER_SECRET_CANARY.env
/app/proxies.txt
/app/config/chains.toml.backup
OWNER_SECRET_CANARY layer marker
```

```text
RUNTIME_IMAGE_CONTEXT_ABSENCE_EXIT=0
```

Only dummy markers were inspected; no actual secret was supplied to the build or printed.

## Real internal-network candidate admission

A fresh local `--internal` Docker network hosted ephemeral PostgreSQL and Redis. A distinct owner migrated zero→`0010` through the explicit migration image. A non-owner `app_runtime` was provisioned locally. The actual runtime image started with a read-only mounted empty-chain registry fixture, so there were no external RPC endpoints. No provider/worker/payout/sweep call was run.

The real runtime container answered:

```text
GET /health -> {"status":"healthy","version":"0.1.0"}
GET /ready  -> {"status":"ready","version":"0.1.0"}
```

Before application command execution, canary launches correctly failed:

```text
OWNER_ADMISSION_EXIT=1 OWNER_MARKER=0
MIGRATION_URL_ADMISSION_EXIT=1 MIGRATION_MARKER=0
```

With Redis stopped:

```text
REDIS_DOWN_READY_STATUS=503
BODY={"detail":"service unavailable"}
HEALTH_STATUS=200
```

With PostgreSQL stopped:

```text
DB_DOWN_READY_STATUS=503
BODY={"detail":"service unavailable"}
```

This is real image/entrypoint/API process evidence, distinct from earlier mocked endpoint/lifespan tests. No production rollout follows from it.

## Deliberately not claimed

The all-worker loop canary batch was bounded by a 120-second outer timeout and did not complete cleanly. It is not passed and is outside this integrated API admission package; no external worker loop was allowed to run to completion. All ephemeral containers and the internal network were removed after the completed API/admission checks.

## Final five-service LOCAL entrypoint gate

The reproducible harness `scripts/run_runtime_candidate_admission.py` was run after the prior API/readiness evidence. It creates only a unique disposable Compose project, containers, volumes and `internal: true` network; cleanup addresses only that project via `docker compose ... down --volumes --remove-orphans`. It builds distinct locally tagged runtime and explicit migration images with the prior approved host-network build transport, uses generated disposable DB credentials, and does not print their values.

The harness writes a unique dummy secret **value** to an ignored `secrets/` file before build. It scans runtime image inspect configuration, image history and exported runtime filesystem for that value. It separately renders each API/poller/webhook/sweeper/expirer configuration with the candidate runtime image and curated runtime env, requiring the dummy runtime canary, no owner marker, no migration URL and no Compose entrypoint override. It migrates a fresh disposable database using the distinct migration image, provisions a disposable non-owner role, then invokes each service only as `python -c 'print("ADMISSION_CANARY_<service>")'`; the runtime image entrypoint remains active. No `python -m` worker is invoked.

Exact successful markers:

```text
RUNTIME_CANDIDATE_IMAGE_CONTEXT_SECRET_VALUE_EXIT=0
RUNTIME_CANDIDATE_COMPOSE_FIVE_SERVICE_RENDER_EXIT=0
RUNTIME_CANDIDATE_FIVE_SERVICE_ENTRYPOINT_CANARIES_EXIT=0
RUNTIME_CANDIDATE_OWNER_AND_MIGRATION_REJECTS_EXIT=0
RUNTIME_CANDIDATE_DYNAMIC_PRIVILEGE_DRIFT_REJECTS_EXIT=0
```

For every one of the five services, the valid non-owner marker exited `0` exactly once. A per-service owner-URL override and a migration-URL override each exited nonzero before their marker. Dynamic disposable `TRUNCATE` on protected `webhooks` and an executable public SECURITY DEFINER function likewise each exited nonzero before their marker. The dynamic guard checks all currently reviewed runtime table names, not just ledger tables.

Wall time was 195.16 seconds, dominated by the two bounded local Docker builds; every per-canary process uses a 20-second timeout. This is local candidate evidence only. It does not establish production permissions, secret delivery, PUBLIC inventory, production migration safety, provider behavior, worker behavior or financial readiness.

## Final source regression subset

With explicit inert `SECRET_KEY` and `ENCRYPTION_KEY` process environment (no dotenv loading), the following non-money test command completed:

```text
pytest -q tests/test_runtime_container_admission.py tests/test_runtime_database_urls.py tests/test_limited_runtime_database_guard.py tests/test_rails.py sdk/python/tests/test_sdk.py
87 passed, 3 skipped, 6 warnings in 1.48s
RUNTIME_AND_SDK_RAIL_REGRESSION_EXIT=0
```

The three skips are isolated PostgreSQL rail JSON-contract tests that require an explicitly provided disposable loopback test database; they are not converted to passes, suppressed, or run against a non-disposable database. The six warnings are existing Pydantic v2 class-based `Config` deprecation warnings from merchant/wallet schemas. No real-money E2E test was selected.

## Superseding final corrections

The first accepted five-service harness used one generated password for owner, runtime, and Redis. That limitation was corrected and the harness rerun once: owner PostgreSQL, non-owner runtime, and Redis credentials are independently generated; the unique owner credential is also scanned as an owner-URL marker and is absent from runtime image configuration/history/filesystem and runtime compose rendering. The corrected five-service markers all passed again (`179.36s` total).

The harness-generated Compose render is now complemented by an automated render of the actual `docker-compose.yml` plus `docker-compose.runtime-role.yml`, supplied only with temporary dummy `--env-file` operator variables and a temporary curated runtime env file. All five actual service names passed runtime-required/owner-exclusion/empty-migration/entrypoint assertions and API `/ready` healthcheck assertion (`ACTUAL_COMPOSE_RENDER_TEST_EXIT=0`). Exact final acceptance and separate `0009`/`0010` regression evidence are in `.ralph/runtime-final-acceptance.md`.
