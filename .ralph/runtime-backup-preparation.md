# Runtime backup preparation — completed approved preparation only

Date: 2026-09-05
Status: backup preparation and restore verification completed. No production role/grant/credential change, migration, push, build, deployment, service restart, application/worker launch, cleanup, provider action, or financial operation occurred.

## Preconditions and provenance

- Deploy skill was read; active GitHub account is `bigidulka`.
- Local runtime candidate is committed locally and not pushed under `security(runtime): isolate limited-role deployment`; final local SHA is available from Git. The exact clean runtime-source test worktree was parent `f6c751239718de33873cd3a910b9342dbac4913f`; later amendments are review/rollout reports only, as previously verified by name-only comparison.
- Remote `docker compose` is `2.39.4`; a temporary dummy-only base-plus-override `!override` render outside the repository passed:

```text
REMOTE_DUMMY_COMPOSE_OVERRIDE_RENDER_EXIT=0
```

- Deployment preflight for `api` exited successfully. Snapshot: `nproc=16`, `load1=16.29`, available memory about `6.6 GiB`, free root filesystem about `60.4 GB` at `74%` used, and API was healthy. Backup script repeated its own bounded preflight and did not block.

## Approved external directory preparation

The director-approved external path was created only because it was absent:

```text
/home/server/.config/arbitron-payment          mode 0700 uid/gid 1000/1000
/home/server/.config/arbitron-payment/backups  mode 0700 uid/gid 1000/1000
```

No existing object was chmodded; no runtime env, database password, application role, or secret value was created or copied. The path is now available for a later approved `0600` runtime file, but that file remains deliberately absent.

## Backup artifact

A consistent PostgreSQL custom-format dump was created by `pg_dump -Fc` inside the current production PostgreSQL container, using only its internal configured database credential. It was written directly to the approved external backup directory; it was not downloaded locally and no table payload/key/user data was read or emitted.

```text
artifact: /home/server/.config/arbitron-payment/backups/runtime-backup-preparation-20260905.dump
mode:     0600
size:     942797 bytes
sha256:   2dea833e3a91165fcd09d386ee59ccde1934b1e7add800900df868a592aed189
manifest: /home/server/.config/arbitron-payment/backups/runtime-backup-preparation-20260905.manifest.json
manifest mode: 0600
```

Safe source snapshot taken around dump creation:

```text
revision:           0009_merchant_rails
public table count: 18
constraint count:   49
estimated live rows:1323
```

`estimated_live_rows` is PostgreSQL statistics, not a payload read and not a byte-for-byte restore-consistency assertion.

## Isolated restore verification

The custom dump was restored with trusted same-system PostgreSQL tools into a uniquely named temporary PostgreSQL container with:

```text
network: none
published ports: none
memory: 1g
cpus: 1
pids limit: 128
production volume mount: none
application/worker connection: none
```

`pg_restore --exit-on-error --no-owner --no-privileges` exited successfully. The safe restored snapshot was:

```text
revision:           0009_merchant_rails
public table count: 18
constraint count:   49
estimated live rows:7301
restore exit:       0
```

The differing estimated-live-row values are normal planner-statistics behavior after restore and are not treated as a record-count mismatch. No sensitive row was selected. The uniquely named restore container and its uniquely named volume were removed after verification; the protected custom dump and manifest remain.

## Captured rollback references

Current production service image IDs were captured read only and must be preserved; no tag/reuse assumption is made:

```text
api             sha256:3a1d9c7b27ebcc344d1d7900a15fc98b7f2f804ab5224961229211c0af6c9f9f
worker-poller   sha256:669c345ace8047c2bdfdf8ac79372eb0d63d3ca86b53e134eb78098d9992f11d
worker-webhook  sha256:5edee79c32d25ac2ae3ee903d8bbb227abb311845a62307e17bb27a0f74313ac
worker-sweeper  sha256:67cf5affa82822107dbe84a98a857108d5993bf6e0383935884d0fa32e294318
worker-expirer  sha256:737093a855b4eb813a5ba3d4f96b5ccf1e98512c6b98d549cc5b95304d41ee34
```

Existing `.rollback/` entry names remain presence-only evidence and are not used as a database recovery claim.

## Current production facts retained for next authorization

- Current schema is still `0009_merchant_rails`; no ledger tables exist.
- Existing `arbitron` is privileged and remains unchanged, migration-admin only by director decision.
- `PUBLIC CREATE=false`, `PUBLIC TEMPORARY=true`, and public SECURITY DEFINER inventory is empty. No common `PUBLIC` revoke is proposed.
- New `arbitron_runtime` role, runtime password, grants, migration `0010`, curated runtime env, push/build/deploy/API canary/workers all remain unperformed.

## Exact pending commands/stages — not authorized yet

1. Director authorizes push/provenance release of the selected candidate SHA and rechecks source equivalence.
2. Director authorizes owner-only `0010_ledger_foundation` one-shot migration after a fresh freeze/inventory check.
3. DBA creates only new `arbitron_runtime` with the reviewed exact post-0010 grant matrix; existing `arbitron` remains unchanged.
4. Deploy user creates the later approved external runtime env file at the approved 0700 directory with mode 0600, without copying the legacy `.env` or any owner connection.
5. Run same-connection role proof, then actual Compose render, API canary, and one worker at a time under a separately approved change window.

No production execution beyond this backup/restore preparation is implied by this report.
