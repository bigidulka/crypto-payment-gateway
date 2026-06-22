# Per-check Address Lease Migration

Миграция payment gateway с persistent per-user deposit addresses на per-check address lease. Цель: снизить количество OKLink/RPC scan targets до active payment checks, безопасно остановить старую систему, перепроверить старые адреса, выполнить sweep drain, изменить DB/logic/tests, провести live tests.

## Goals
- Безопасно остановить legacy scanner/sweeper перед cutover.
- Перепроверить все legacy addresses, вставить missing deposits idempotent, довести sweep до нуля/non-dust.
- Реализовать DB migration для `deposit_address_pool`, `payment_checks`, lease events и связи deposits.
- Реализовать per-check address lease flow и scanner по active checks.
- Добавить unit/DB/integration tests.
- Подготовить и выполнить controlled deploy/live test plan без прямых source edits на сервере.

## Checklist
- [x] Зафиксировать текущий local/server state и dirty/staged changes, не потерять чужие изменения.
- [x] Проверить server compose/services names и подготовить точные stop commands для legacy workers.
- [ ] Согласовать/выполнить остановку legacy scanner/sweeper после preflight. **Blocked: нужен выбор full freeze vs worker-only freeze.**
- [ ] Снять DB backup/export state before migration. **Blocked until stop/freeze decision.**
- [x] Спроектировать DB migration и модели address pool/payment checks/lease events.
- [x] Реализовать Alembic migration + SQLAlchemy models.
- [x] Реализовать address lease service: acquire/release/cooldown/late deposit policy.
- [x] Перевести payment/check creation на leased address.
- [x] Перевести scanner на active checks only, OKLink primary, no RPC logs fallback.
- [x] Реализовать legacy address recheck/backfill command.
- [x] Реализовать sweep drain command/report for legacy addresses.
- [x] Добавить tests: lease, DB constraints, scanner, idempotency, late deposits.
- [x] Запустить targeted tests/lint.
- [x] Подготовить deploy instructions + live BSC canary steps.
- [ ] Выполнить live test только после явного безопасного checkpoint.

## Verification
- Local state captured:
  - repo `/home/fsdf1234/Projects/arbitron-payment-gh`, branch `main`, HEAD `26932ad`.
  - worktree is heavily dirty with pre-existing staged changes: deleted legacy backend files, new `.env.example`, modified `src/core/config.py`, etc.
  - our current uncommitted OKLink/per-check prep touches unstaged tracked paths and new files; staging not modified.
- Server state captured:
  - path `/home/server/Projects/arbitron-payment`, branch `main`, HEAD `825800b`.
  - server dirty files: `src/api/deps.py`, `src/blockchain/resilient_fetcher.py`, `src/workers/persistent_poller.py`, `tmp/`.
  - compose file: `docker-compose.yml`.
- Server services:
  - compose services: `redis`, `postgres`, `worker-expirer`, `worker-persistent`, `worker-poller`, `worker-sweeper`, `worker-webhook`, `api`.
  - running containers: API/Postgres/Redis healthy, worker containers unhealthy due known healthcheck issue.
  - worker commands:
    - `worker-persistent`: `python -m src.workers.persistent_poller`
    - `worker-poller`: `python -m src.workers.evm_log_poller`
    - `worker-sweeper`: `python -m src.workers.unified_sweeper_runner`
    - `worker-webhook`: `python -m src.workers.webhook_dispatcher`
    - `worker-expirer`: `python -m src.workers.invoice_expirer`
- Server preflight snapshot:
  - `nproc=16`, load `13.91 13.21 11.13`.
  - root disk 73% used, 61G free.
  - arbitron-payment container CPU/mem low; persistent ~7% CPU.
  - Docker df: images 10.23GB, volumes 85.87GB.
- DB read-only state snapshot:
  - active EVM wallet addresses: `532` on each of `arbitrum`, `avax`, `base`, `bsc`, `optimism`, `polygon`; `13` each on `solana`, `ton`.
  - deposit counts: arbitrum `47`, avax `19`, base `15`, bsc `317`, optimism `15`, polygon `13`.
  - unified sweep jobs: `completed=413`; no pending/failed observed in grouped output.
- DB/model implementation checks:
  - Added Alembic migration `alembic/versions/20260622_000001_per_check_address_leases.py`.
  - Added enums in `src/db/models/enums.py`: `DepositAddressLeaseStatus`, `PaymentSessionStatus`.
  - Extended `src/db/models/payment.py`: reusable lease fields on `DepositAddress`, status/expires/release timestamps on `PaymentSession`, new `AddressLeaseEvent` model.
  - Exported new models/enums from `src/db/models/__init__.py`.
  - Command: `PYTHONPATH=. uv run --no-project --with ruff ruff check --select E501,F401,I001 src/db/models/enums.py src/db/models/payment.py src/db/models/__init__.py alembic/versions/20260622_000001_per_check_address_leases.py` — PASS.
  - Command: model import smoke with SQLAlchemy deps — PASS (`address_lease_events`, `available`, `pending`, lease/session cols present).
  - Command: `alembic heads` — PASS, head `0007_per_check_address_leases`.
  - Command: `alembic history -r 0005:head` — PASS, chain `0005 -> 0006 -> 0007`.
- Address lease/payment flow implementation checks:
  - Added `src/services/address_lease_service.py` with `acquire_available_address`, `acquire_address_by_id`, `bind_payment_session`, `release_to_cooldown`, `mark_late_deposit`, `promote_ready_cooldowns`, active-session query.
  - Updated `src/services/payment_service.py`: payment option creation now acquires `DepositAddress` lease, sets `PaymentSession.status=PENDING`, copies `invoice.expires_at`, binds audit event, and releases lease to cooldown after confirmed payment/sweep job creation.
  - Updated `src/workers/evm_log_poller.py`: active address query now uses `PaymentSession.status/expires_at`, marks session `SEEN_ONCHAIN`, and eager-loads deposit address for confirmation flow.
  - Updated active-check scanner to respect `scanner_provider`; OKLink path uses `OKLinkTransferLogFetcher`, does not fallback to RPC logs on OKLink error/incomplete scan, and does not advance checkpoint when incomplete.
  - Updated invoice expiry paths (`src/workers/invoice_expirer.py`, `src/services/invoice_service.py`) to release active payment-session leases to cooldown when invoice expires manually or by worker.
  - Address cooldown policy is derived from invoice config: `invoice.expires_at + invoice.ttl_minutes`.
  - Added `tests/test_address_lease_service.py` covering acquire, cooldown promotion/reuse, release to cooldown, and lease events.
  - Added `tests/test_evm_log_poller_oklink.py` covering OKLink active-check scan checkpoint advance and incomplete-scan no-advance/no-RPC-fallback behavior.
  - Added `aiosqlite` to dev dependencies for async SQLite DB tests.
  - Command: `PYTHONPATH=. uv run --no-project --with ruff ruff check --select E501,F401,F841,I001 src/services/address_lease_service.py src/services/payment_service.py src/workers/evm_log_poller.py tests/test_address_lease_service.py src/db/__init__.py src/db/models/__init__.py src/db/models/enums.py src/db/models/payment.py alembic/versions/20260622_000001_per_check_address_leases.py` — PASS.
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings pytest tests/test_address_lease_service.py -q` — PASS (`3 passed`, one dependency deprecation warning).
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings --with cryptography --with mnemonic --with hdwallets --with redis --with arq pytest tests/test_address_lease_service.py tests/test_evm_log_poller_oklink.py -q` — PASS (`5 passed`, one dependency deprecation warning).
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings --with cryptography --with mnemonic --with hdwallets --with redis --with arq pytest tests/test_persistent_poller_oklink.py tests/test_address_lease_service.py tests/test_evm_log_poller_oklink.py -q` — PASS (`7 passed`, one dependency deprecation warning).
  - Command: `PYTHONPATH=. uv run --no-project --with ruff ruff check --select E501,F401,F841,I001 src/workers/evm_log_poller.py src/workers/invoice_expirer.py src/services/invoice_service.py src/services/address_lease_service.py src/services/payment_service.py tests/test_evm_log_poller_oklink.py tests/test_address_lease_service.py` — PASS.
  - Command: `git diff --check` for touched lease/payment/scanner/test/task files — PASS.
  - Command: `alembic heads` — PASS, head `0007_per_check_address_leases`.
  - Command: service import smoke for `AddressLeaseService`/`PaymentService` with runtime deps — PASS.
- Legacy maintenance implementation checks:
  - Added `src/maintenance/legacy_backfill.py`: fixed-range OKLink-backed recheck for legacy `wallet_addresses`, idempotent insert through `UserWalletService.record_deposit`, no checkpoint advance, no writes unless `execute=True`, no processing when OKLink scan incomplete.
  - Added CLI `scripts/legacy_backfill.py`: requires explicit `--chain`, `--from-block`, `--to-block`; dry-run default, `--execute` for writes.
  - Added `src/maintenance/legacy_sweep_drain.py`: DB-side report for legacy deposit/sweep drain state and optional missing `UnifiedSweepJob` creation for confirmed persistent deposits.
  - Added CLI `scripts/legacy_sweep_drain.py`: report default, `--create-missing-jobs --execute` for writes, optional `--chain`/`--limit`.
  - Added `tests/test_legacy_maintenance.py`: legacy backfill idempotent insert, incomplete scan no-write, sweep drain report + missing job creation.
  - Command: `PYTHONPATH=. uv run --no-project --with ruff ruff check --select E501,F401,F841,I001 src/maintenance scripts/legacy_backfill.py scripts/legacy_sweep_drain.py tests/test_legacy_maintenance.py` — PASS.
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings --with cryptography --with mnemonic --with hdwallets --with redis --with arq pytest tests/test_legacy_maintenance.py -q` — PASS (`3 passed`, one dependency deprecation warning).
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings --with cryptography --with mnemonic --with hdwallets --with redis --with arq pytest tests/test_persistent_poller_oklink.py tests/test_address_lease_service.py tests/test_evm_log_poller_oklink.py tests/test_legacy_maintenance.py -q` — PASS (`10 passed`, one dependency deprecation warning).
  - Command: CLI import/help smoke for `scripts/legacy_backfill.py --help` and `scripts/legacy_sweep_drain.py --help` — PASS.
  - Command: `git diff --check` for maintenance scripts/modules/tests/task file — PASS.
- Late-payment policy hardening:
  - Added `PaymentService.process_late_payment`: expired invoice remains `EXPIRED`, session becomes `LATE`, one low-priority invoice sweep job is created idempotently for manual reconciliation.
  - Updated `evm_log_poller.update_confirmations`: confirmed tx for `PaymentSessionStatus.LATE` or `InvoiceStatus.EXPIRED` uses late-payment path instead of auto-confirming invoice.
  - Fixed late-detection scan gap: expired/late sessions are still scanned while their address is in cooldown (`lease_status=cooldown`, `cooldown_until > now`), then excluded after cooldown/address reuse.
  - Hardened reused-address late attribution: if multiple terminal sessions share the same address during current cooldown, scanner maps the address to the most recent terminal session by `released_at/paid_at/chosen_at`.
  - Added `tests/test_late_payment_policy.py`: late payment creates sweep without confirming invoice; repeated late processing does not create duplicate sweep/event; expired session is scanned only during address cooldown; reused address maps to latest terminal session.
  - Command: `PYTHONPATH=. uv run --no-project --with ruff ruff check --select E501,F401,F841,I001 src/services/payment_service.py src/workers/evm_log_poller.py tests/test_late_payment_policy.py` — PASS.
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings --with cryptography --with mnemonic --with hdwallets --with redis --with arq pytest tests/test_late_payment_policy.py tests/test_evm_log_poller_oklink.py -q` — PASS (`6 passed`, one dependency deprecation warning).
  - Command: targeted combined suite now includes late tests — PASS (`17 passed`, one dependency deprecation warning).
- Deploy/canary planning:
  - Added `.ralph/deploy-live-canary-plan.md` with freeze/backup/deploy/post-migration checks, legacy backfill/drain dry-run/execute commands, BSC live canary, late-deposit canary, rollback notes.
- DB constraints and broader targeted checks:
  - Added model-side partial unique index `uq_payment_session_address_active` on `PaymentSession.deposit_address_id` for active statuses (`pending`, `seen_onchain`) with PostgreSQL and SQLite predicates, matching Alembic migration intent and enabling SQLite constraint tests.
  - Added `tests/test_payment_session_constraints.py`: verifies model partial index metadata, rejects two active sessions for one address, allows address reuse after terminal sessions.
  - Added `.ralph/dirty-state-commit-plan.md` with staged/unstaged risk inventory, migration bucket list, and safe staging/deploy prep commands.
  - Command: `PYTHONPATH=. uv run --no-project --with pytest --with pytest-asyncio --with sqlalchemy --with aiosqlite --with greenlet --with httpx --with web3 --with eth-account --with pydantic-settings --with cryptography --with mnemonic --with hdwallets --with redis --with arq pytest tests/test_payment_session_constraints.py -q` — PASS (`3 passed`, one dependency deprecation warning).
  - Command: combined targeted suite (`test_persistent_poller_oklink.py`, `test_address_lease_service.py`, `test_evm_log_poller_oklink.py`, `test_legacy_maintenance.py`, `test_late_payment_policy.py`, `test_payment_session_constraints.py`) — PASS (`17 passed`, one dependency deprecation warning).
  - Command: broad targeted Ruff over touched migration/services/workers/maintenance/tests — PASS.
  - Command: `alembic heads` + `alembic history -r 0005:head` — PASS, head `0007_per_check_address_leases`, chain `0005 -> 0006 -> 0007`.
  - Command: `git diff --check` for DB constraint test/dirty-state/task files — PASS.
- Migration hardening after SQL review:
  - Generated offline SQL for `0006_migrate_sweep_data -> 0007_per_check_address_leases` to `tmp/migration_backups/0007_per_check_address_leases.sql` and reviewed it.
  - Fixed migration so active sessions force `deposit_addresses.lease_status='leased'`, `is_used=true`, `leased_until=expires_at`.
  - Fixed migration so terminal sessions (`paid`, `expired`, `late`, `cancelled`) move addresses to `cooldown` if `expires_at + ttl_minutes > now()`, otherwise `available/is_used=false`.
  - Fixed migration so addresses with no sessions become `available/is_used=false` unless retired.
  - Command: `alembic upgrade 0006_migrate_sweep_data:0007_per_check_address_leases --sql > tmp/migration_backups/0007_per_check_address_leases.sql` — PASS.
- Dirty-state isolation prep:
  - Added backup artifacts under `tmp/migration_backups/`:
    - latest `per_check_tracked_20260622T094249Z.patch` for tracked unstaged migration diff.
    - latest `per_check_untracked_20260622T094249Z.tgz` for untracked migration files/docs/tests.
    - previous `per_check_tracked_20260622T093924Z.patch` / `per_check_untracked_20260622T093924Z.tgz` kept.
  - Updated `.ralph/dirty-state-commit-plan.md` with backup artifact paths and note that `.env`/unrelated staged deletions are excluded.
  - Added `.ralph/migration-review-manifest.md` with SHA256 checksums, untracked archive contents, tracked diff stat, mixed-file caveats, and validation result.
  - Verified `per_check_tracked_20260622T094249Z.patch` matches current tree with `git apply --reverse --check`; direct clean apply is not valid because patch is relative to current dirty index.
  - Recorded mixed-file staging needs: `.env.example` OKLink block, `pyproject.toml` `aiosqlite`, `src/core/config.py` OKLink settings must be staged with `git add -p`.
  - Command: combined targeted suite — PASS (`17 passed`, one dependency deprecation warning).
  - Command: broad targeted Ruff over touched migration/services/workers/maintenance/tests — PASS.

## Reflection checkpoint — iteration 6
- Accomplished:
  - OKLink primary scanner for persistent and active-check flows.
  - Per-check address lease DB migration/models/service.
  - Payment creation uses leased address; expiry/confirmation releases address to cooldown.
  - Active-check scanner avoids RPC log fallback when OKLink provider configured.
  - Legacy backfill and sweep drain maintenance commands exist with dry-run default.
  - Tests cover lease lifecycle, OKLink scanner completeness, legacy idempotent backfill, sweep drain, and late-payment no auto-confirm policy.
- Working well:
  - Targeted test suite is fast and isolated with SQLite/fakes.
  - Server/prod actions are still read-only/not executed.
  - Design reuses existing `invoices` + `payment_sessions`; no extra `payment_checks` table needed.
- Blocking:
  - Production freeze/backup still blocked on user choice (`full freeze` recommended).
  - Local repo and server repo are dirty; deploy needs git state cleanup/reconciliation.
  - Official OKLink endpoints still need valid key/access for production-grade bulk/history use.
- Approach adjustment:
  - Keep code work local until freeze decision.
  - Do not attempt live migration/deploy before resolving dirty states and DB backup.
  - Treat late payments as expired/manual-reconciliation path: never auto-confirm expired invoice.
- Next priorities:
  - Add DB migration/constraint tests and run broader targeted tests.
  - Resolve local staged/dirty state into safe commit plan.
  - After explicit user choice: full freeze, backup/export, deploy/canary.

## Reflection checkpoint — iteration 11
- Accomplished since previous reflection:
  - Migration SQL reviewed offline and hardened for active/terminal/no-session address state.
  - Late-deposit handling now covers cooldown scan window and reused-address attribution.
  - DB constraint test added for one active session per leased address.
  - Migration diffs archived locally without `.env` or unrelated staged deletions.
  - Deploy/canary plan and dirty-state commit plan are documented.
- Working well:
  - Core code path is now test-covered: lease acquire/release, scanner completeness, legacy backfill/drain, late payment, DB active-session constraint.
  - Safety posture holds: no server source edits, no production stop, no secrets printed.
  - Offline Alembic SQL review caught real migration-state bug before deploy.
- Still blocking:
  - Production freeze/backup/export not possible until user chooses freeze mode; full freeze remains recommended.
  - Local repo index contains large unrelated staged work; commit/deploy requires isolating migration bucket or fresh clean worktree.
  - Server repo is dirty, so `git pull --ff-only` deploy will block until server state is reconciled.
- Approach adjustment:
  - Stop adding broad features; focus on final isolation/review and production cutover once freeze is approved.
  - Keep legacy backfill/drain dry-run first; execute only after DB backup and scan-range choice.
  - Do not attempt live BSC canary until deploy commit is isolated, pulled, migrated, and services restarted under explicit checkpoint.
- Next priorities:
  - Get explicit `full freeze` vs `worker-only freeze` decision.
  - If approved: stop services, take pg_dump/export, then deploy from clean commit.
  - If not approved: prepare clean patch/branch for user review without touching existing staged unrelated changes.
- Reflection verification:
  - Command: no `TODO/FIXME/placeholder/stub` in touched migration/service/worker/maintenance/test files — PASS.
  - Command: `alembic heads` and `alembic history -r 0005:head` — PASS, head `0007_per_check_address_leases`.
  - Command: `git diff --check` over touched migration/service/worker/maintenance/test/docs files — PASS.
  - Command: combined targeted suite — PASS (`17 passed`, one dependency deprecation warning).

## Prepared Stop Commands (NOT EXECUTED)

Full freeze for migration (prevents new payments and all money-moving workers, keeps DB/Redis):

```bash
cd /home/server/Projects/arbitron-payment
docker compose stop api worker-persistent worker-poller worker-sweeper worker-webhook worker-expirer
```

Worker-only freeze (keeps API online, but unsafe if API can create new payments):

```bash
cd /home/server/Projects/arbitron-payment
docker compose stop worker-persistent worker-poller worker-sweeper worker-webhook worker-expirer
```

Resume if rollback needed:

```bash
cd /home/server/Projects/arbitron-payment
docker compose up -d api worker-persistent worker-poller worker-sweeper worker-webhook worker-expirer
```

Backup command to run after stop/pre-migration checkpoint:

```bash
cd /home/server/Projects/arbitron-payment
mkdir -p tmp/migration_backups
docker exec arbitron-payment-postgres pg_dump -U arbitron -d arbitron_payment -Fc > tmp/migration_backups/arbitron_payment_$(date -u +%Y%m%dT%H%M%SZ).dump
```

State export command to run after stop/pre-migration checkpoint:

```bash
cd /home/server/Projects/arbitron-payment
mkdir -p tmp/migration_backups
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -Atc "select chain,address,last_scanned_block from wallet_addresses where is_active=true order by chain,address" > tmp/migration_backups/active_wallet_addresses_$(date -u +%Y%m%dT%H%M%SZ).tsv
```

## Design Decision
- Не создаём отдельную таблицу `payment_checks`: существующие `invoices` + `payment_sessions` уже реализуют check/session abstraction.
- Existing `deposit_addresses` becomes address pool; `PaymentSession` becomes per-check lease record.
- New `address_lease_events` provides audit trail.
- Persistent `user_wallets/wallet_addresses/deposits` remain legacy-only for recheck/backfill and slow drain.

## Notes
- Server authoritative path: `/home/server/Projects/arbitron-payment`.
- Local repo for code work: `/home/fsdf1234/Projects/arbitron-payment-gh`.
- Do not print secrets or `.env` contents.
- Do not edit source files directly on server; server actions operational only.
- Stopping production services causes downtime; require explicit command checkpoint before actual stop if not already fully confirmed.
- Existing OKLink primary scanner work exists in local tree; staging is mixed/pre-existing.
- Next safe production step: ask/confirm full freeze vs worker-only freeze, then run backup/export.
- Next local implementation step: wait for freeze decision or isolate migration bucket into clean review branch/patch without disturbing pre-existing staged work.
- Iteration 12 note: review manifest added; tracked backup patch is current-index-relative rollback/review artifact, not standalone clean-HEAD patch.
- Iteration 8 note: late-deposit scanner window now covers expired sessions during address cooldown; this fixes gap where late tx after invoice expiry would never be recorded.
- Iteration 9 note: reused-address late attribution now picks latest terminal session, preventing old expired sessions from stealing late deposits during a newer cooldown.
- Iteration 10 note: offline migration SQL review found old terminal invoice addresses would remain `is_used=true` and never be reused; migration now normalizes active/terminal/no-session address lease state.
