# Runtime role security review — P0 correction scope

Date: 2026-09-05
Status: local correction in progress. No commit, push, production write, role/grant, credential rotation, restart, migration, provider operation, financial feature enablement, or deployment is authorized.

## Rejected candidate assumptions

1. Setting `MIGRATION_DATABASE_URL=""` in a compose service is insufficient while `env_file: .env`, owner `DATABASE_URL`, or database-owner/bootstrap secrets remain visible to a limited runtime container.
2. A limited runtime configuration must not silently keep the shared owner environment. The limited topology needs an explicit opt-in compose override/profile with a curated runtime-only environment/secret source. It must provide the runtime connection as `DATABASE_URL` too, so existing helpers cannot accidentally consult an owner URL.
3. `init_db()` / `Base.metadata.create_all()` is forbidden in limited runtime mode. Schema work is an external one-shot migration-owner operation.
4. A role provisioning template must fail closed for existing elevated, member, owner, database-owner, schema-owner, object/function-owner, direct/effective privileged role states. It must not silently demote a role, revoke unrelated grants, rotate a password, or claim a password change when the role already exists.
5. `TEMPORARY`/`CREATE` inherited through `PUBLIC` need effective privilege checks. Production `PUBLIC` changes are out of scope.

## Required local evidence

* dummy compose configuration demonstrates limited containers have no owner marker, migration owner URL, or PostgreSQL owner/bootstrap secret;
* settings tests use `_env_file=None` plus monkeypatched environment to prevent ambient `.env` influence;
* isolated PostgreSQL negative provisioning tests cover superuser, membership, database owner, schema/table/function owner, direct `TRUNCATE`, and effective `PUBLIC CREATE`, asserting failure leaves no partial role/grant state;
* happy provisioning applied twice is idempotent; app role completes legacy invoice/outbox plus ledger posting on one connection and cannot DDL/truncate/disable triggers;
* runtime mode fails closed on database privilege/search-path/bootstrap violations;
* report exact results, no claim of production readiness.

Provider contracts, source/verification lifecycle, backfill, fees, payouts, reconciliation, actual production readiness and rollout remain outside this correction.

## Final local candidate evidence

`docker-compose.runtime-role.yml` is an opt-in Compose-spec `!override`: it replaces base runtime service env configuration with an operator-selected curated file, sets `DATABASE_RUNTIME_ROLE_ENABLED=true`, and clears `MIGRATION_DATABASE_URL` for API and all four workers. The actual base-plus-override render test now scrubs host `COMPOSE_*`, database, Redis, PostgreSQL, and runtime-env variables before invoking Compose, then validates five real services against only temporary dummy input files.

`DATABASE_URL` remains compatible while limited mode is disabled. Limited runtime uses its supplied non-owner `DATABASE_URL`; Alembic reads a distinct `MIGRATION_DATABASE_URL` only outside runtime. Runtime schema bootstrap is forbidden. The shared runtime guard rejects elevated ownership/DDL state, executable public SECURITY DEFINER functions, and `TRUNCATE` across the reviewed runtime table inventory.

The provisioning template is transaction-scoped and fail-closed: it does not rotate/demote existing roles, rejects elevated/membership/ownership/effective privilege states, does not create future default grants, and is tested only against disposable loopback `test_*` databases. The test derives the psql database name from its explicit test URL rather than hard-coding it.

Final accepted results—including actual image/entrypoint admission, isolated role-template/guard tests, actual Compose render, and separate `0009`/`0010` regression sets—are in `.ralph/runtime-final-acceptance.md`. The earlier real-worker loop timeout remains not passed. Production privilege inventory, role names, secret delivery, deployment, finance, and any `PUBLIC` change remain deferred. No production action is claimed or authorized.
