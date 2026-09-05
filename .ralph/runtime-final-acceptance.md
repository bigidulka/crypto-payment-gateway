# Runtime candidate — final LOCAL acceptance checkpoint

Date: 2026-09-05
Status: candidate evidence complete for review. No production action occurred: no production read/write, role/grant/credential action, schema rollout, deployment, restart, push, provider/RPC request, worker loop, payout, or financial operation. The reviewed local candidate was committed without push; exact SHA is recorded below.

## Accepted local runtime gate

The corrected `scripts/run_runtime_candidate_admission.py` was run once after independent credential separation:

```text
RUNTIME_CANDIDATE_IMAGE_CONTEXT_SECRET_VALUE_EXIT=0
RUNTIME_CANDIDATE_COMPOSE_FIVE_SERVICE_RENDER_EXIT=0
RUNTIME_CANDIDATE_FIVE_SERVICE_ENTRYPOINT_CANARIES_EXIT=0
RUNTIME_CANDIDATE_OWNER_AND_MIGRATION_REJECTS_EXIT=0
RUNTIME_CANDIDATE_DYNAMIC_PRIVILEGE_DRIFT_REJECTS_EXIT=0
```

Wall time: 179.36 seconds; each container canary has its own 20-second bound. The unique local Compose project and its internal network, containers, volumes, images, temp files, and ignored dummy secret file were cleaned by the harness. No real `python -m` worker was used.

The local harness generates three different disposable values: PostgreSQL owner password (also the unique owner-URL marker), non-owner application password, and Redis password. The owner marker/value is present only in the disposable migration environment and transient negative overrides. Runtime image inspection, history, and exported filesystem were each checked for both the unique dummy secret value and unique owner-password/URL marker; neither was present. Runtime and migration candidate image IDs are distinct.

The valid non-owner entrypoint-preserving `python -c` marker exited `0` exactly once for API, poller, webhook, sweeper, and expirer. For each service an owner database URL and migration URL override exited nonzero before its marker. Dynamic protected-table `TRUNCATE` and executable `PUBLIC` SECURITY DEFINER drift also exited nonzero before their markers.

## Actual base + override Compose render

`tests/test_runtime_container_admission.py::test_limited_runtime_compose_override_replaces_base_env_file` renders the actual repository files:

```text
docker compose --env-file <temporary operator.env> \
  -f docker-compose.yml -f docker-compose.runtime-role.yml config --format json
```

The test uses only temporary dummy operator variables plus a temporary curated `RUNTIME_ENV_FILE`; it does not use repository `.env` values. It verifies the five real deployment service names:

```text
api
worker-poller
worker-webhook
worker-sweeper
worker-expirer
```

For each, it verifies runtime database/Redis values, `SECRET_KEY`, `ENCRYPTION_KEY`, runtime canary, enabled limited-role flag, empty/absent migration URL, no `POSTGRES_PASSWORD`, no owner database/Redis/PostgreSQL marker, and no Compose entrypoint override. The API healthcheck is `/ready`.

Result:

The local render evidence used `docker compose` version `5.4.0`.

```text
6 passed, 6 existing Pydantic warnings
ACTUAL_COMPOSE_RENDER_TEST_EXIT=0
```

After the final subprocess environment scrub (`COMPOSE_*`, database, Redis, PostgreSQL, and `RUNTIME_ENV_FILE` removed before `docker compose`), the actual render test was rerun with explicit inert application settings:

```text
6 passed, 6 existing Pydantic warnings
ACTUAL_COMPOSE_SCRUBBED_RENDER_EXIT=0
```

This is distinct from the harness's generated `compose.json`: the generated file proves isolated candidate runtime process admission; this test proves the actual base-plus-override merge excludes inherited owner env content. README documents that the limited override requires current `docker compose` with Compose-spec `!override`; legacy `docker-compose` v1 is unsupported.

## Separate isolated database regressions

All test environments provided explicit inert `SECRET_KEY` and `ENCRYPTION_KEY`; application Settings did not rely on dotenv loading.

### Legacy and SDK at exact `0009_merchant_rails`

A disposable loopback PostgreSQL database named `test_final_legacy_0009` was migrated exactly to `0009_merchant_rails`. The legacy suite and SDK suite ran there with explicit destructive-reset safeguards and isolated rail database URL.

```text
267 passed, 33 skipped, 6 deselected, 7 warnings in 5.44s
LEGACY_SDK_0009_FINAL_ISOLATED_EXIT=0
```

Excluded by exact scope: all ledger tests, the Anvil/money E2E files, chain E2E file, role-template test, and the six direct public RPC balance nodes `tests/test_modules.py::TestRPCConnectivity::test_can_check_balance`. The 33 existing skips remain reported, not converted to green. Warnings: six Pydantic v2 class-`Config` deprecations plus one existing `TestResult` collection warning.

### Ledger at exact `0010_ledger_foundation`

A different disposable loopback database named `test_final_ledger_0010` was migrated exactly to `0010_ledger_foundation`. Ledger foundation, database integrity, posting service, and legacy invoice API compatibility tests ran there. The API compatibility test now binds both cached Settings/DB engine and Redis pool to explicit local disposable dependencies, uses real loopback Redis for `/ready`, and continues to mock only RPC/lifecycle operations unrelated to readiness; no external RPC is contacted.

```text
13 passed, 6 existing Pydantic warnings in 2.10s
LEDGER_0010_ISOLATED_EXIT=0
```

A first run exposed two test-isolation defects rather than a source suppression: retained rows after an interrupted run and `/ready` using a cached ambient DB/Redis client. The ledger database was recreated before the accepted rerun; the API compatibility test now explicitly resets only its process-local settings/engine/Redis caches and cleans them in `finally`.

### Runtime role provisioning and admission guards at separate `0010`

A third disposable loopback database, `test_final_role_0010`, was migrated to `0010_ledger_foundation` and supplied through explicit role-template variables. The role-template test no longer hard-codes a database name: its `psql` invocation uses the explicit, loopback `test_*` URL's database. No ambient `DATABASE_RUNTIME_ROLE_ENABLED`, runtime URL, or migration URL was inherited.

```text
24 passed, 6 existing Pydantic warnings in 2.15s
RUNTIME_ROLE_PROVISIONING_ISOLATED_EXIT=0
```

This includes template happy/idempotent and negative paths plus runtime URL, dynamic privilege-guard, and actual base+override Compose tests. The pre-existing same-connection application rehearsal remains a candidate script and was not newly claimed in this checkpoint.

The two unique named local regression containers were stopped after tests. No generic Docker deletion command addressed unrelated containers.

## Candidate source/change review

Runtime-security/root source touched in this candidate includes:

```text
Dockerfile
docker-entrypoint.sh
docker-compose.yml
docker-compose.runtime-role.yml
.dockerignore
README.md
src/core/config.py
src/db/session.py
src/main.py
alembic/env.py
scripts/provision_application_runtime_role.sql
scripts/run_runtime_candidate_admission.py
tests/conftest.py
tests/test_runtime_container_admission.py
tests/test_runtime_database_urls.py
tests/test_limited_runtime_database_guard.py
tests/test_application_runtime_role_template.py
tests/test_ledger_legacy_api_compatibility.py
```

No owner environment file, owner URL, or real credential is retained in the candidate source/harness. The harness's temporary credentials are generated per run, never printed, and removed. `scripts/provision_application_runtime_role.sql` remains a reviewed template only; it must **not** be applied blindly to production.

Other uncommitted candidate reports/scripts/tests are separately visible in `git status` and require review as a set. `tmp/` and `uv.lock` remain untracked workspace artifacts, not accepted runtime implementation files.

Integrity at prior checkpoint: selected Ruff checks and format checks passed; `git diff --check` passed; accepted foundation source `e41349cc5c39cd247644f1eb1e66edfa08671d16` remains an ancestor. The reviewed runtime candidate is committed locally under `security(runtime): isolate limited-role deployment` with no push; its final SHA is reported by the release checkpoint, not embedded here.

## Boundary before release decision

Production remains at `0009_merchant_rails`; this local evidence does not alter it. Before any production authorization, a director must separately review the committed candidate diff, production backup/recovery plan, exact production role/object/PUBLIC/SECURITY DEFINER inventory, secret delivery, migration-owner procedure, curated runtime environment, API canary, and then staged worker rollout. No global `PUBLIC` revoke is proposed without that inventory.
