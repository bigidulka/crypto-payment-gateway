# Limited runtime-role rollout plan — proposed only

Status: **not authorized to execute**. This is a director-review artifact. No production DDL, migration, role/grant/credential change, secret file, restart, image build, deployment, worker launch, ledger backfill, or financial feature enablement was performed while preparing it.

## Read-only inventory basis

Guarded, read-only inspection of `/home/server/Projects/arbitron-payment` found:

- deployed source SHA: `5f5c0ca84a4d6328275957739e2a800d0fadc86c`; unrelated remote untracked paths exist (`.rollback/`, `data/inventory/`, `data/smoke/`, `tmp/`);
- Docker Compose `2.39.4`, which supports the candidate Compose-spec override requirement;
- current schema revision `0009_merchant_rails`; no ledger tables;
- one non-system application role, `arbitron`, with `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `INHERIT`, `REPLICATION`, and `BYPASSRLS`; all 18 public tables are owned by it;
- no application-role memberships were found; `PUBLIC CREATE` on `public` is false; `PUBLIC TEMPORARY` is true; no public-schema SECURITY DEFINER functions were found;
- public tables: `address_lease_events`, `alembic_version`, `api_keys`, `chain_checkpoints`, `deposit_addresses`, `deposits`, `invoice_events`, `invoices`, `merchants`, `onchain_txs`, `outbox_webhooks`, `payment_sessions`, `rails`, `unified_sweep_jobs`, `user_balances`, `user_wallets`, `wallet_addresses`, `webhooks`; no public sequences;
- all five deployed runtime Compose services declare an `env_file` and explicit `DATABASE_URL`, `REDIS_URL`, `TZ` keys. Values were not read;
- existing rollback directory `.rollback/` exists and is writable by the remote deployment user. This is presence-only evidence, not a verified backup or restore test;
- candidate secret-delivery paths `/etc/arbitron`, `/etc/arbitron-payment`, `/run/secrets`, and `/home/server/.config/arbitron-payment` were absent. No path was created and no permissions were changed;
- pre-change API/worker image IDs were read and recorded during inventory. Preserve them in the approved rollback record before any replacement; do not delete old images.

## Director decisions recorded after inventory

- Existing privileged `arbitron` remains unchanged and is migration-admin only; it must never enter API/worker runtime environment.
- New `arbitron_runtime` is the future application login role.
- Leave `PUBLIC TEMPORARY` unchanged. Reconfirm `PUBLIC CREATE=false` and empty public SECURITY DEFINER inventory immediately before rollout. No global `PUBLIC` revoke is authorized or proposed.
- Approved external secret base path is `/home/server/.config/arbitron-payment`, owned by verified deploy user `server` (uid/gid `1000/1000`), not root. The base and `backups/` directory were created because absent, both mode `0700`; no runtime env/credential was created. A later runtime env must be mode `0600` under this directory.
- `0010_ledger_foundation` remains unused additive schema: no ledger calls, backfill, fees, payouts, or provider features are enabled by the migration.
- Backup preparation and isolated restore evidence is in `.ralph/runtime-backup-preparation.md`. It is an approved backup checkpoint, not approval for migration/deploy.

## Required director inputs before any command

1. Approved final local `security(runtime): isolate limited-role deployment` commit (no push), signed/verified release provenance, and final clean checkout test record.
2. Named production migration owner: retain existing `arbitron` **only as one-shot migration owner**, never in runtime service env. It remains highly privileged and is not a runtime identity.
3. New unique runtime login name: `arbitron_runtime`. Do not repurpose, rotate, demote, or grant the existing `arbitron` role.
4. Approved secret-delivery facility and an absolute path outside the repository, owned by the approved service owner, mode `0600`. The read-only inventory found no approved candidate path; its creation is an explicit later operation, not implied by this plan.
5. Verified logical/physical backup, checksum, restore rehearsal, operator, retention, and rollback decision owner. `.rollback/` presence alone is insufficient.
6. Exact production object/PUBLIC inventory recheck immediately before execution. The current `PUBLIC TEMPORARY=true` must be evaluated by the director; no global `PUBLIC` revoke is proposed here.

Read-only pre-change image IDs captured for rollback reference:

```text
api             sha256:3a1d9c7b27ebcc344d1d7900a15fc98b7f2f804ab5224961229211c0af6c9f9f
worker-poller   sha256:669c345ace8047c2bdfdf8ac79372eb0d63d3ca86b53e134eb78098d9992f11d
worker-webhook  sha256:5edee79c32d25ac2ae3ee903d8bbb227abb311845a62307e17bb27a0f74313ac
worker-sweeper  sha256:67cf5affa82822107dbe84a98a857108d5993bf6e0383935884d0fa32e294318
worker-expirer  sha256:737093a855b4eb813a5ba3d4f96b5ccf1e98512c6b98d549cc5b95304d41ee34
```

The existing `.rollback/` directory contains `20260702_153950_treasury_to_funder` and `proxy-pool`. Names and directory presence were read only; they are not proof of a database backup or recovery capability.

## Exact clean-checkout evidence

The initial clean detached worktree used parent candidate `f6c751239718de33873cd3a910b9342dbac4913f`. The later local amendment adds final evidence/rollout documentation only; release-critical runtime source, Compose, Docker, SQL-template, and test files are unchanged between that tested tree and the amended candidate. The release director must verify this equivalence (`git diff --name-only f6c7512..HEAD`) and the final candidate SHA before any authorization.

```text
CLEAN_RUNTIME_CONFIG_GUARD_EXIT=0       20 passed, 7 warnings
CLEAN_SDK_RAIL_0009_EXIT=0              71 passed
CLEAN_LEGACY_0009_SUBSET_EXIT=0         14 passed, 3 warnings
CLEAN_LEDGER_0010_EXIT=0                13 passed, 6 warnings
CLEAN_ROLE_TEMPLATE_0010_EXIT=0         4 passed
RUNTIME_CANDIDATE_*                     all five final image/entrypoint markers passed
```

The clean runtime config set includes actual base-plus-override Compose render with host `COMPOSE_*`, database, Redis, PostgreSQL, and runtime-env variables scrubbed. The `0010` API compatibility test used a real disposable loopback Redis endpoint for `/ready`; RPC lifecycle dependencies were mocked and no external RPC was contacted. The five `RUNTIME_CANDIDATE_*` markers came from one exact-SHA local host-network Docker build plus isolated internal-network short-marker harness run; no real worker loop was launched.

## Production inventory result and remaining unknown

Read-only production inspection established: revision `0009_merchant_rails`; all 18 listed public tables owned by privileged `arbitron`; no public sequences; `PUBLIC CREATE=false`; `PUBLIC TEMPORARY=true`; no public-schema SECURITY DEFINER functions; no ledger table. The same guarded inspection captured current runtime image IDs above and confirmed all five runtime services currently declare an `env_file` plus `DATABASE_URL`, `REDIS_URL`, and `TZ` source keys.

Secret directory readiness remains a precise **unknown/not ready**: no approved external candidate path was present among the inspected locations, and no owner/path/mode `0600` facility has been approved or created. This plan intentionally does not set up passwords, directories, or secrets.


## Proposed command stages — placeholders only

Commands below intentionally use placeholders and must be executed only in an approved change window. They must be supplied through an approved secret mechanism, never shell history, repository env files, logs, or `docker compose config` output.

### Stage 0 — freeze and capture

```text
cd /home/server/Projects/arbitron-payment
# Record only: git SHA, git status, Compose version, API + each worker image ref/ID,
# current Alembic revision, role/object/PUBLIC inventory, backup verification identifier.
# Abort if SHA/status/revision/inventory differs from approved values.
```

Do not pull, build, restart, or migrate in this stage.

### Stage 1 — verified backup and recovery checkpoint

```text
# Run the separately approved backup command as the approved database operator.
# Verify artifact checksum, access control, and restore rehearsal identifier.
# Abort on failure; retain current images and runtime env unchanged.
```

No database change is permitted until this stage has written a reviewed backup record.

### Stage 2 — one-shot migration owner only

Build/use the explicit migration target, not the default runtime image:

```text
# Inputs injected by approved secret delivery only:
# MIGRATION_DATABASE_URL=<existing arbitron owner connection>
# SECRET_KEY=<approved application secret>
# ENCRYPTION_KEY=<approved encryption key>

docker build --target migration -t arbitron-payment:migration-<approved-sha> .
docker run --rm \
  --env-file <approved-migration-owner-env-outside-repo> \
  arbitron-payment:migration-<approved-sha> \
  alembic upgrade 0010_ledger_foundation
```

Afterward, read only `alembic_version`, ledger integrity definitions, and object ownership. Abort on any revision other than `0010_ledger_foundation`. The migration owner container must not be used for API or workers.

### Stage 3 — create and verify new non-owner role

An approved DBA must adapt and execute the reviewed template logic for the exact inventory:

```text
# New role only: arbitron_runtime
# Existing owner remains migration-only: arbitron
# Inputs: approved generated runtime password and exact reviewed table grant matrix.
# Candidate reference: scripts/provision_application_runtime_role.sql
```

Do **not** apply the local template blindly. Before and after application, verify with the DBA's approved read-only queries:

- `arbitron_runtime` is not superuser/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS and has `NOINHERIT`;
- it has no memberships, database/schema/relation/function ownership, database/schema `CREATE`, executable public SECURITY DEFINER function, or protected-table `TRUNCATE`;
- exact `search_path` is `public, pg_catalog`;
- its DML grants match the reviewed runtime table matrix after `0010`;
- `PUBLIC TEMPORARY` is separately assessed. Do not revoke it globally unless an approved impact inventory and exact grant/revoke plan exists.

### Stage 4 — curated runtime environment and same-connection proof

Create the approved runtime-only env outside the repository with mode `0600` and service-owner ownership. It contains only:

```text
DATABASE_RUNTIME_ROLE_ENABLED=true
DATABASE_URL=<arbitron_runtime connection>
REDIS_URL=<runtime Redis connection>
SECRET_KEY=<application secret>
ENCRYPTION_KEY=<encryption secret>
# other documented runtime-only application values as reviewed
```

It must contain neither `MIGRATION_DATABASE_URL` nor `POSTGRES_*`, `PGPASSWORD`, owner database URLs, or bootstrap values.

Before deployment, pass the complete application permission proof using **one `arbitron_runtime` connection** against the approved post-`0010` test/canary environment: legacy invoice/outbox DML and ledger posting, then verify no DDL, `TRUNCATE`, trigger disable, ownership, or admission-guard bypass. Repeat entrypoint admission for all five service roles/services using short marker commands only; do not launch real workers during this proof.

Render actual compose only with a controlled operator variable file:

```text
docker compose --env-file <approved-operator-vars-outside-repo> \
  -f docker-compose.yml -f docker-compose.runtime-role.yml config
```

Inspect names/structure under the approved secure procedure without exporting secret values. Abort if any runtime service inherits an owner/migration/bootstrap variable or an entrypoint override.

### Stage 5 — image and API canary

```text
git checkout --detach <approved-sha>
docker build -t arbitron-payment:runtime-<approved-sha> .
docker compose --env-file <approved-operator-vars-outside-repo> \
  -f docker-compose.yml -f docker-compose.runtime-role.yml up -d api
```

Verify API limited-role entrypoint admission and `/ready`; confirm `/health` is liveness only. Keep the prior API image ID and prior curated configuration reference for rollback. Abort and restore the previous API image/config if the canary fails. No worker is changed in this stage.

### Stage 6 — workers one at a time

Only after API canary acceptance, deploy and observe separately:

```text
worker-poller
worker-webhook
worker-sweeper
worker-expirer
```

For each: start one service, verify limited-role entrypoint admission and service health/log criteria without exposing secrets, observe the approved interval, then decide whether to proceed. Stop escalation on the first anomaly. No ledger backfill, payout, sweep policy change, or provider feature activation is part of this rollout.

### Stage 7 — rollback

Rollback is a director decision. Preserve prior API/worker image IDs and pre-change runtime configuration reference securely. If a schema rollback is needed, use the separately approved and restore-tested recovery procedure; do not assume `alembic downgrade` is safe for the immutable ledger migration. Never delete backup artifacts or old images during the rollout window.

## Director approval checklist

- [ ] approve source SHA and clean detached-checkout evidence;
- [ ] approve backup/restore evidence;
- [ ] approve `arbitron` migration-only / new `arbitron_runtime` mapping;
- [ ] approve secret-delivery path, ownership, and `0600` lifecycle;
- [ ] approve exact post-`0010` grants and PUBLIC impact assessment;
- [ ] approve all-five same-connection permission proof;
- [ ] approve API canary and one-by-one worker sequence;
- [ ] approve rollback owner and conditions.
