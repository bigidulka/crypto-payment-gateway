# Runtime role operation policy — candidate review

Status: LOCAL candidate policy only. This document is not a production runbook authorization. No production role, grant, credential, migration, image, container, deployment, restart, or financial operation is authorized or executed by this checkpoint.

## Configuration compatibility

`Settings` now uses `env_file=None`: application startup no longer discovers a repository or current-directory `.env` implicitly. This is intentional so a limited runtime container cannot inherit an owner environment by accident.

Local CLI and test startup must therefore explicitly supply required application variables from the caller's environment or an explicitly selected env-file mechanism outside the application:

```text
set -a; . ./local-runtime.env; set +a
python -m <explicit-command>
```

The selected local file must provide at least `SECRET_KEY` and `ENCRYPTION_KEY`; a normal runtime also needs its explicitly selected `DATABASE_URL` and `REDIS_URL`. Tests install inert process-local defaults in `tests/conftest.py` before imports and use `_env_file=None` where they construct `Settings`; they do not rely on an ambient dotenv file.

`DATABASE_RUNTIME_ROLE_ENABLED=false` preserves legacy `DATABASE_URL` migration behavior. When a reviewed non-owner runtime topology is enabled, the runtime process receives only the non-owner `DATABASE_URL`; its entrypoint rejects nonempty `MIGRATION_DATABASE_URL` and effective owner/bootstrap environment names. Alembic is an explicit external migration-owner operation using `MIGRATION_DATABASE_URL`, never a default command in the runtime image.

## Runtime image and compose policy

- Default Dockerfile image is application runtime; the migration image requires `--target migration`.
- `docker-compose.runtime-role.yml` is an opt-in override which requires `RUNTIME_ENV_FILE`; it replaces inherited service env configuration and clears `MIGRATION_DATABASE_URL` for all five runtime services.
- The curated runtime env must not contain migration-owner URLs, PostgreSQL owner/bootstrap keys, or owner markers.
- The candidate admission harness uses unique disposable Docker Compose names, a fresh internal-only network, dummy non-secret values, and short `python -c` marker commands. It preserves the runtime image entrypoint and never launches a real worker module.

## Proposed production sequence — do not execute from this document

1. Obtain a verified backup and restore/recovery evidence under the approved production process.
2. Inventory exact current database roles, ownership, object grants, `PUBLIC` privileges, SECURITY DEFINER functions and effective runtime permissions. Do **not** execute a broad `REVOKE ... FROM PUBLIC`; only prepare narrowly justified remediation after the inventory.
3. Under the approved migration-owner process, run the reviewed one-shot migration to `0010` and verify its revision and integrity checks. This must not be performed by a runtime container.
4. Review actual owner/runtime role identities and secret delivery, then adapt—not blindly apply—`scripts/provision_application_runtime_role.sql` to the inventoried production state. Preserve rollback and abort on unexpected ownership/privilege state.
5. Create and review a curated runtime-only environment: non-owner `DATABASE_URL`, runtime Redis/application secrets, no owner/bootstrap/migration connection values.
6. Launch one API candidate under the curated runtime environment. Verify limited-role admission and `/ready`; do not infer this from liveness alone.
7. After API canary acceptance, launch each worker separately using the same curated runtime topology. Observe operational behavior under the approved external-dependency controls.
8. Keep migration-owner credentials and image separate from all runtime services. Any rollback decision requires the approved restoration and role/credential procedure.

The current local checks do not grant production approval. In particular, no production `PUBLIC` privilege action is proposed without the inventory in step 2.
