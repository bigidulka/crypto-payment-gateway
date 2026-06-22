# Per-check Migration Review Manifest

Generated during Ralph iteration 12.

## Backup artifacts

| Artifact | SHA256 | Purpose |
|---|---:|---|
| `tmp/migration_backups/per_check_tracked_20260622T094249Z.patch` | `16ad2acc8fed7a7871386c1f91aa0740224e281d4bc062489ab9ff75863a78b6` | Tracked unstaged migration diff against current index |
| `tmp/migration_backups/per_check_untracked_20260622T094249Z.tgz` | `1adbc5daae50a65bcd3f28f37540f36a2658a1a36e1f3a547db346f7cef72f8d` | Untracked migration files/docs/tests |
| `tmp/migration_backups/0007_per_check_address_leases.sql` | `7a8f131b4a89e526cb4c30379d209a4359f9fffc2b6927c83a62d6d2a3d039bf` | Offline Alembic SQL for review |

## Patch validation

- `git apply --reverse --check tmp/migration_backups/per_check_tracked_20260622T094249Z.patch` — PASS.
- Plain `git apply --check` against current dirty worktree fails because patch is already applied and is relative to current index, not clean HEAD.
- Use backup as rollback/review artifact, not direct clean-HEAD deployment patch.

## Untracked archive contents

```text
.ralph/deploy-live-canary-plan.md
.ralph/dirty-state-commit-plan.md
.ralph/oklink-primary-migration.md
.ralph/per-check-address-migration.md
alembic/versions/20260622_000001_per_check_address_leases.py
scripts/legacy_backfill.py
scripts/legacy_sweep_drain.py
src/blockchain/oklink_client.py
src/maintenance/__init__.py
src/maintenance/legacy_backfill.py
src/maintenance/legacy_sweep_drain.py
src/services/address_lease_service.py
tests/test_address_lease_service.py
tests/test_evm_log_poller_oklink.py
tests/test_late_payment_policy.py
tests/test_legacy_maintenance.py
tests/test_oklink_client.py
tests/test_payment_session_constraints.py
tests/test_persistent_poller_oklink.py
```

## Mixed files requiring `git add -p`

- `.env.example`
  - staged: whole file creation, no OKLink section.
  - unstaged migration change: OKLink scanner env block only.
- `pyproject.toml`
  - staged: project name change.
  - unstaged migration change: `aiosqlite>=0.20.0` dev dependency.
- `src/core/config.py`
  - staged: app/database defaults rename.
  - unstaged migration change: OKLink settings only.

## Current migration tracked diff stat

```text
.env.example                     |   9 ++
config/chains.toml               |  36 +++++
pyproject.toml                   |   1 +
src/blockchain/chains.py         |  21 ++-
src/core/config.py               |   9 ++
src/db/__init__.py               |   6 +
src/db/models/__init__.py        |  12 +-
src/db/models/enums.py           |  20 +++
src/db/models/payment.py         | 126 ++++++++++++++++-
src/services/invoice_service.py  |  19 ++-
src/services/payment_service.py  | 182 +++++++++++++++---------
src/workers/evm_log_poller.py    | 298 ++++++++++++++++++++++++++++++++++-----
src/workers/invoice_expirer.py   |  29 +++-
src/workers/persistent_poller.py |  85 +++++++++--
14 files changed, 729 insertions(+), 124 deletions(-)
```

## Deployment implication

Need clean migration commit before server deploy. Current index includes unrelated staged deletes/renames; do not commit current index as-is.
