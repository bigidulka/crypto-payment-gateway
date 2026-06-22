# Dirty State / Commit Prep Plan

Repo: `/home/fsdf1234/Projects/arbitron-payment-gh`

## Current risk
- Worktree has large pre-existing staged changes unrelated to per-check migration.
- Do not commit blindly.
- Do not include secrets or `.env` deletion in migration commit.
- Mixed files have both staged and unstaged changes: `.env.example`, `pyproject.toml`, `src/core/config.py`.

## Pre-existing staged bucket — keep separate / do not include blindly
Observed staged examples:
- `D .env` — do not commit unless explicitly intended.
- `D backend/**`, `D data/arbitron_payment.db`, `D rpc`, `D config/chains.toml.backup`.
- Frontend/docs/scripts broad edits.
- Existing staged changes in `pyproject.toml`, `src/core/config.py`, `.env.example` overlap with this migration.

## Migration bucket to commit after review
Core OKLink + config:
- `.env.example` (partial/mixed; stage carefully)
- `config/chains.toml`
- `src/blockchain/chains.py`
- `src/blockchain/oklink_client.py`
- `src/core/config.py` (partial/mixed; stage carefully)
- `tests/test_oklink_client.py`
- `tests/test_persistent_poller_oklink.py`

Lease DB/model/service:
- `alembic/versions/20260622_000001_per_check_address_leases.py`
- `src/db/__init__.py`
- `src/db/models/__init__.py`
- `src/db/models/enums.py`
- `src/db/models/payment.py`
- `src/services/address_lease_service.py`
- `src/services/payment_service.py`
- `src/services/invoice_service.py`
- `src/workers/invoice_expirer.py`
- `src/workers/evm_log_poller.py`

Legacy maintenance:
- `src/maintenance/__init__.py`
- `src/maintenance/legacy_backfill.py`
- `src/maintenance/legacy_sweep_drain.py`
- `scripts/legacy_backfill.py`
- `scripts/legacy_sweep_drain.py`

Tests/dev deps:
- `pyproject.toml` (`aiosqlite`, mixed; stage carefully)
- `tests/test_address_lease_service.py`
- `tests/test_evm_log_poller_oklink.py`
- `tests/test_legacy_maintenance.py`
- `tests/test_late_payment_policy.py`
- `tests/test_payment_session_constraints.py`

Planning docs, optional not deploy code:
- `.ralph/oklink-primary-migration.md`
- `.ralph/per-check-address-migration.md`
- `.ralph/deploy-live-canary-plan.md`
- `.ralph/dirty-state-commit-plan.md`

## Backup artifacts

Latest local isolation backup created in project temp dir:
- `tmp/migration_backups/per_check_tracked_20260622T094249Z.patch` — tracked unstaged migration diff only.
- `tmp/migration_backups/per_check_untracked_20260622T094249Z.tgz` — untracked migration files/docs/tests archive.

Previous backup kept:
- `tmp/migration_backups/per_check_tracked_20260622T093924Z.patch`
- `tmp/migration_backups/per_check_untracked_20260622T093924Z.tgz`

These backups intentionally exclude `.env` and unrelated staged deletions.

Review manifest:
- `.ralph/migration-review-manifest.md` records SHA256 sums, tar contents, mixed-file caveats, and validation results.

Important caveat:
- `per_check_tracked_20260622T094249Z.patch` is relative to current index. `git apply --reverse --check` passes on current tree, but direct apply to clean HEAD is not expected to pass until unrelated staged base changes are reconciled.

## Safe commit prep commands

Inspect staged vs unstaged before touching index:

```bash
git diff --cached --name-status
git diff --name-status
git status --short
```

Recommended approach:
1. Save external patch of current unstaged migration work for backup:
   ```bash
   mkdir -p tmp/migration_backups
   git diff > tmp/migration_backups/per_check_unstaged_$(date -u +%Y%m%dT%H%M%SZ).patch
   ```
2. Ask user before altering index because index already contains unrelated staged work.
3. Prefer new clean worktree or branch if possible.
4. If using current index, stage only migration bucket with `git add -p` for mixed files:
   ```bash
   git add -p .env.example pyproject.toml src/core/config.py
   git add config/chains.toml src/blockchain/chains.py src/blockchain/oklink_client.py
   git add alembic/versions/20260622_000001_per_check_address_leases.py
   git add src/db/__init__.py src/db/models/__init__.py src/db/models/enums.py src/db/models/payment.py
   git add src/services/address_lease_service.py src/services/payment_service.py src/services/invoice_service.py
   git add src/workers/evm_log_poller.py src/workers/invoice_expirer.py src/workers/persistent_poller.py
   git add src/maintenance scripts/legacy_backfill.py scripts/legacy_sweep_drain.py
   git add tests/test_oklink_client.py tests/test_persistent_poller_oklink.py tests/test_address_lease_service.py tests/test_evm_log_poller_oklink.py tests/test_legacy_maintenance.py tests/test_late_payment_policy.py tests/test_payment_session_constraints.py
   ```
5. Verify staged diff contains no secrets and no unrelated deletions:
   ```bash
   git diff --cached --check
   git diff --cached --stat
   git diff --cached -- . ':!*.patch'
   ```

## Deployment blocker
- Production deploy should wait until this migration bucket is isolated in a clean commit and pushed.
- Server dirty state must be resolved before `git pull --ff-only`.
