# Schema-role preparation script — LOCAL review checkpoint

Date: 2026-09-05
Status: local code and unit review only. The script was not pushed, copied remotely, or executed against production. No remote production operation occurred in this checkpoint.

## Reviewed files

```text
scripts/prepare_runtime_schema_role.py
tests/test_prepare_runtime_schema_role.py
alembic/env.py
```

## Current mutable preparation implementation

`prepare_runtime_schema_role.py` contains an executable mutable phase machine guarded by `prepare --confirm-prepare`: check, owner-only migration, post-migration verification, durable pending credential, new-role provisioning, role verification, raw runtime-env publication, and complete/failure journaling. It contains no Compose `up`, `restart`, `stop`, `down`, worker, or API service-switch command. The full local happy run passed; operational production use still awaits final review/commit/authorization.

The phase vocabulary remains explicit for the later reviewed implementation:

```text
checked -> migrated -> pending_password -> role_created -> runtime_env_published -> complete
failure records phase=failed while preserving last_completed_phase
```

Journals are cumulative, mode `0600`, link-count checked, fsynced with their parent directory, and preserve previous safe fields plus `last_completed_phase`. The local core refuses a preexisting journal/pending/runtime file before Docker/database probing: automatic crash resume is intentionally absent. An operator must inspect recovery state explicitly.

Secure file helpers now reject symlinks, directories, FIFO/devices, wrong owner/mode, and hard links. They use exclusive `O_NOFOLLOW` creation, complete-write loops, file and parent-directory `fsync`; owned cleanup verifies the original device/inode and therefore refuses a replaced path. Newline-bearing environment values are rejected. Compose is changed to `env_file.format: raw`, so dollar signs, quotes, hash, whitespace, Unicode and backslash values retain literal env-file semantics; the actual base-plus-override Compose JSON render asserts the literals for all five services. Direct `compose run` execution was not added because it invokes a bridge-network build and hits the already-known local Debian package transport failure; this is recorded as blocked rather than passed.

Image presence is no longer provenance: `check` requires configured immutable migration and runtime image IDs equal `docker image inspect .Id`, exact Git SHA, nonempty protected backup with exact checksum, secure directories, and no recovery paths. The later mutable stage must additionally verify actual network/container mapping and database role/revision/PUBLIC gates before DDL.

`alembic/env.py` retains bounded transaction-local migration lock/statement timeouts and now doubles `%` before `ConfigParser` storage. The real special-character owner-password migration connection still needs a disposable happy test, not a source-string assertion.

## Tests run — local-only

```text
pytest -q tests/test_prepare_runtime_schema_role.py tests/test_runtime_database_urls.py
10 passed in 0.02s
MUTABLE_PHASE_MACHINE_UNIT_EXIT=0

pytest -q tests/test_prepare_runtime_schema_role.py tests/test_runtime_container_admission.py tests/test_runtime_database_urls.py
18 passed, 6 existing Pydantic warnings in 1.37s
SCHEMA_ROLE_PHASE_CORE_FINAL_EXIT=0
```

Injected/pure tests cover no shell/service-switch command surface, immutable image-ID mismatch, source/recovery refusal ordering, secure regular-file/FIFO/symlink behavior, cumulative journal failure state, inode-safe cleanup, secret-free subprocess errors, raw env literal serialization/newline refusal, actual base-plus-override Compose JSON raw literal rendering for all five services, bounded timeout source, and percent interpolation source safeguard.

### Real mutable happy attempts — diagnostic evidence preserved

The fixture uses fresh uniquely tagged `--network=host` local builds, a unique internal network, PostgreSQL network alias `postgres`, an actual percent-encoded special-character owner URL in a 0600 bootstrap env file, silent sleeping source containers, immutable image/source-manifest checks, and no application or worker module.

The corrected verifier now uses structured catalog rows—four exact `relkind='r'` ledger tables, zero rows, three named `plpgsql` trigger-returning non-security-definer functions with `proconfig=['search_path=pg_catalog, pg_temp']`, exact ten enabled trigger mappings, and named constraint presence—rather than `pg_get_functiondef` text formatting. Unit evidence:

```text
12 passed in 0.03s
STRUCTURED_MIGRATION_VERIFIER_UNIT_EXIT=0
```

The one allowed full happy repeat produced preserved safe evidence:

```text
SCHEMA_ROLE_REAL_HAPPY_EXIT=1
stage=mutable preparation
diagnostic=.ralph/schema-role-happy-diagnostic-ca17c37738c6.json
PREPARATION_FAILED: post-migration verification: deferred_constraints
manifest_phase=failed
last_completed_phase=migrated
failure_class=PreparationError
db_revision=0010_ledger_foundation
runtime_role_exists=false
pending_exists=false
runtime_file_count=0
source_container_ids_unchanged=true for all five
```

This does **not** indicate migration corruption. The ledger final-state constraint triggers are intentionally deferrable and initially deferred; `pg_constraint` includes their trigger-generated constraint rows. The verifier incorrectly rejected every deferred constraint rather than restricting its expectation to the two named final-state trigger constraints. No threshold was lowered, no additional retry was made, and no role/env/canary/zero-row success is claimed.

### Full real local happy run — passed

The deterministic final local fixture run passed:

```text
SCHEMA_ROLE_REAL_HAPPY_EXIT=0
```

It used fresh local `--network=host` runtime/migration Dockerfile builds, a unique internal network with PostgreSQL alias `postgres`, special-character percent-encoded owner URL, an initial actual `0009` migration, and five silent inert source containers with matching settings. The mutable phase machine then completed `0009 -> 0010`, exact structured catalog validation, new `arbitron_runtime` role provisioning/flag verification, durable pending-password lifecycle, and five raw curated runtime env files mode `0600`. The fixture verified zero rows in all ledger tables, owner-side revision `0010_ledger_foundation`, source container IDs unchanged, no pending/migration env artifact remaining, and unique fixture cleanup.

The actual candidate runtime image entrypoint executed a read-only runtime-role canary. It read only `current_user`, `current_database`, `current_setting('search_path')`, and counts on granted ledger transaction/entry tables; it did not query ungranted `alembic_version`, start an application/worker, or contact a provider. Its marker asserted:

```text
current_user=arbitron_runtime
current_database=test_schema_role
search_path=public, pg_catalog
ledger_transactions=0
ledger_entries=0
raw HD seed literal preserved=true
```

Canary exceptions now produce only a whitelisted `CANARY_FAILURE|<exception-class>|<SQLSTATE-or-none>` marker. No raw traceback, query parameters, URL, password, or full environment is emitted.

The role template was not broadened: `alembic_version` remains ungranted, and the owner fixture separately verified revision/zero ledger rows. No service was switched or started.

### Real post-role publish-failure recovery — passed

One second fresh isolated fixture invoked the real `prepare` Python function directly with a harness-only `publish_exclusive` injection: first `runtime-api.env` published, then the next publication raised controlled `OSError`. No production CLI flag or fault interface exists in the operational script.

```text
SCHEMA_ROLE_REAL_PUBLISH_FAILURE_EXIT=0
```

Observed safe recovery facts before fixture cleanup:

```text
journal phase=failed
last_completed_phase=role_created
pending credential exists and mode=0600
api runtime env exists and mode=0600
runtime env count=1 (only the known first partial publication)
owned migration env absent
operation lock absent
new runtime role authenticates read-only as arbitron_runtime
all five source container IDs unchanged
subsequent check refuses recovery paths (no automatic recreate/rotation)
```

The fixture then removed only its own containers, network, images and temporary directory. No service was switched and no production object was touched.

## Remaining gaps — deliberately not claimed

- The real happy preparation/canary and real post-role publish-failure recovery now passed in isolated local fixtures. Production review/commit/authorization remains required.
- The special-character owner URL passed both initial `0009` bootstrap and full mutable `0010` local preparation.
- No script commit/push or production retry is authorized in this checkpoint.
- The current remote remains distinct: source `e9b997c`, old runtime containers/images from `5f5c0ca`, DB `0009`; no service switch has happened.
- No service switch, runtime canary, migration, role, password, or external runtime file was created remotely by this local-only work.
