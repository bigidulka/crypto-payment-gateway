# Runtime schema and role preparation — stopped before mutation

Date: 2026-09-05
Status: director-authorized schema/role stage did **not** begin database or credential mutation because the first remote orchestration attempt failed locally in its Python process before it created the ephemeral migration env or invoked Docker migration/psql provisioning.

## Completed authorized prerequisites

- Local provenance was verified: `e41349c`, `eadc441`, `cdd389a`, and `5a0073a` are ancestors of local main; `f6c7512..cdd389a` contains only `.ralph/runtime-final-acceptance.md` and `.ralph/runtime-production-rollout-plan.md`.
- Active GitHub account was `bigidulka`.
- Reviewed main was pushed; remote `git pull --ff-only` reached `e9b997c0ea740ca974440ef427c71989ed9bd538` (bounded migration lock/statement timeout change).
- Remote preflight passed twice; API remained healthy. No service was recreated or restarted.
- Distinct exact-source images were built without `up` or recreate:

```text
migration: sha256:3aee4e2f2e611a48804f3b9531e468b8993757097c6c063397a8128fd07e83a3
runtime:   sha256:0b412fe71675571acc175ba337423705895a4499b15f83879dae02201891c53b
```

- A new protected consistent custom backup was created before planned DDL:

```text
path:   /home/server/.config/arbitron-payment/backups/runtime-pre-ddl-20260905111920-2314cf5c.dump
sha256: b03f64e865c0b60c4e212c0230eb033f6e82e7563d63dac9c423b00672aeebf4
size:   942797 bytes
pre-DDL revision: 0009_merchant_rails
pre-DDL ledger absent: true
```

## Exact stop condition

The remote orchestration Python command raised:

```text
NameError: name 'POSTGRES_USER' is not defined
```

Cause: shell quoting corrupted a complex inline Python expression before the operation reached the first mutable step. This is an orchestration defect, not a database migration/role failure. The command stopped before writing its ephemeral migration env, creating the pending password file, invoking the migration image, creating `arbitron_runtime`, or writing any `runtime-*.env` file.

Immediate read-only verification after failure confirmed:

```text
revision:       0009_merchant_rails
ledger_exists:  false
runtime_exists: false
```

Thus there is no partial schema, role, credential, runtime file, service, PUBLIC privilege, or existing-owner change to clean up. Existing `arbitron` remains unchanged.

## Required next action

Do not retry this complex inline orchestration command. A purpose-built local operational script must first be written, reviewed, tested against disposable PostgreSQL, and committed/pushed before a new director-authorized production schema-role attempt. The script must avoid shell interpolation of secret-bearing values, use secure ephemeral files only, preserve all already-approved gates, and retain the current no-service-switch boundary.

No API or worker canary is authorized until that reviewed script completes schema/role/runtime-env preparation and all postchecks.
