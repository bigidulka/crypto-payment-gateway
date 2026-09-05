# Ledger foundation local acceptance gate

Date: 2026-09-05  
Status: local pre-commit gate in progress; no production authorization.

## Candidate scope

Expected tracked foundation files only:

```text
alembic/versions/20260905_000003_ledger_foundation.py
src/db/models/__init__.py
src/db/models/ledger.py
src/ledger/__init__.py
src/ledger/amounts.py
src/services/ledger_posting_service.py
tests/test_ledger_foundation.py
tests/test_ledger_db_integrity.py
tests/test_ledger_posting_service.py
.ralph/ledger-foundation-progress.md
.ralph/ledger-review-findings.md
.ralph/ledger-foundation-acceptance.md
```

The legacy shared fixture `tests/conftest.py` must be byte-identical to `HEAD`.

## Required database separation

* **Ledger suites:** fresh isolated `test_ledger_*` database migrated zero → `0010_ledger_foundation`; only explicit `LEDGER_TEST_DATABASE_URL`, `LEDGER_OWNER_DATABASE_URL`, and `LEDGER_SERVICE_DATABASE_URL` may target it. These tests never use the legacy truncation fixture.
* **Legacy plus SDK suite:** a separately created fresh `test_legacy_0009` database migrated only to `0009_merchant_rails`, with existing explicit guarded fixture variables. `tests/test_e2e_chains.py` is excluded because it may move real value.
* No test is permitted to bypass ledger triggers or point legacy `TRUNCATE ... CASCADE` cleanup at `0010`.

## Foundation acceptance already evidenced

* self-contained additive `0010`, frozen migration amount bound, exact runtime/DDL numeric semantics;
* global canonical asset vs tenant account separation, composite tenant FKs and global source uniqueness;
* deferred balanced journal, immutable accounts/assets/journal, posted timestamp invariant, trigger-based truncate block, safe search-path/public relation references, temp-shadow regression;
* caller-owned savepoint posting, standalone wrapper, canonical idempotency and non-leaking source collision behavior;
* empty downgrade allowed and posted-journal downgrade refused.

## Pending outside this foundation gate

* deployment of a dedicated non-owner runtime database role and privilege rollout; database owner/superuser is still trusted administration;
* runtime ORM tables have no explicit schema, so deployment-controlled application search path must resolve them to `public`;
* asset/network canonical validation beyond foundation identity storage (including EVM checksum/canonical policy, Solana/TON asset policy) belongs to future approved asset-registry/lifecycle work;
* immutable source facts/verification decisions, invoice/outbox integration, EVM dual-write/backfill, fees, providers, payouts, reconciliation and all production rollout work.

## Pre-commit isolated results

The first separated legacy run exposed a real foundation-model portability defect: PostgreSQL-only `'NaN'::numeric` in the shared ORM check prevented unrelated SQLite tests from constructing `Base.metadata`. The migration preserves a frozen PostgreSQL-aware NaN rejection while the shared ORM expression uses portable `CAST(amount_atomic AS TEXT) <> 'NaN'`; both retain positive, exact-integer and `< 10**78` rules. The PostgreSQL focused test asserts the exact normalized installed migration constraint plus the corresponding shared ORM clauses.

Fresh separated disposable databases were recreated after that correction:

```text
test_ledger_0010: zero -> 0010_ledger_foundation
test_legacy_0009: zero -> 0009_merchant_rails
```

Ledger-only suites, with all explicit `LEDGER_*_DATABASE_URL` variables targeting `test_ledger_0010`:

```text
12 passed in 1.56s
LEDGER_0010_PARITY_EXIT=0
```

Legacy plus SDK suite, with only existing guarded `TEST_DATABASE_URL` targeting `test_legacy_0009`:

```text
255 passed, 36 skipped, 20 warnings in 10.87s
LEGACY_0009_FINAL_EXIT=0
```

Ledger suites have no pytest skips. The only excluded test is `tests/test_e2e_chains.py`, explicitly excluded because it can move real value. The 36 legacy skips and 20 warnings are pre-existing suite behavior; no suppression was added.

## Commit and verification record

Local foundation candidate before this verification-record update: `d28f5aeb9ae951e9844c15e9ef6985ef967949a0` (`feat(ledger): add immutable journal foundation`). It is local only: no push, deploy, production database migration, runtime-role rollout, or financial feature enablement occurred.

Detached clean checkout at that candidate SHA used a fresh `test_ledger_commit_exact` database, migrated zero → `0010_ledger_foundation`, passed install imports (`LEDGER_INSTALL_IMPORTS_OK`), and completed:

```text
12 passed in 1.54s
DETACHED_EXACT_FOCUSED_EXIT=0
DETACHED_EXACT_POSTED_DOWNGRADE_EXIT=1
DETACHED_EXACT_EMPTY_DOWNGRADE_EXIT=0
```

This acceptance-record update is now included in the final local amend; it does not change production code/schema semantics. The amended SHA is rechecked in a new detached checkout before this gate is closed. No local commit is a production approval.
