# Ledger foundation `0010` — refined ADR and acceptance matrix

Date: 2026-09-05  
Status: approved for a **minimal local ledger-only slice**. No payment observation ingestion, payment attempts, rail lifecycle, feature flag wiring, routes, provider call, backfill, payout, production migration, commit, push or deploy is included in this implementation chunk.

## Confirmed baseline

* Local Alembic head is `0009_merchant_rails` (`0009_merchant_rails (head)` verified before writing SQL).
* `0010_ledger_foundation` is additive and `down_revision = "0009_merchant_rails"`. `0009` is not edited.
* Current EVM scanner/session/sweep behavior, underpayment tolerance, overpayment handling and existing fee/commission charging point are untouched.
* Existing failed/internal webhook rows are unrelated to this schema and are not read or changed.

## Minimal scope

Implement only:

1. global canonical asset identities;
2. explicit tenant-owned ledger accounts;
3. append-only journal headers and lines;
4. a posting service that creates one balanced posted transaction atomically;
5. direct-SQL and service-level isolated PostgreSQL tests.

External source identity/digest is required on the journal header in this slice, but it is **not** an observation lifecycle. A future source subsystem will add immutable observation facts and immutable verification decisions separately.

## Implementation and evidence status

The following table is status reporting, not a claim that every target is already enforced. The initial package was rejected because it claimed pending work as completed. See `.ralph/ledger-review-findings.md` for the director review and substage order.

| Constraint | Current implementation / evidence status |
|---|---|
| Fact separate from verification decision | **Implemented boundary only.** `0010` has source reference/digest and no mutable verification field. Immutable source-fact and decision tables are future work. |
| Exact atomic amounts | **Implemented and evidenced.** Runtime uses the shared `10**78` bound; the immutable migration freezes an equivalent local constant to remain historical/self-contained. Unrestricted `NUMERIC` with explicit non-NaN/positive/integral/range check prevents scale-zero coercion rounding. |
| Composite tenant FKs / shared source | **Implemented and evidenced.** Valid two-merchant parents prove both composite FKs; global source uniqueness produces precise `23505`/`uq_ledger_transaction_source`. Service source conflict does not read or expose foreign ownership. |
| Balance + immutability | **Implemented and evidenced at DB layer.** Deferred final-state guards reject open/empty, unbalanced and cross-asset-only postings; independent asset groups pass. Accounts/assets/entries are immutable; posted header rules, append lock/rejection, truncate guard, non-owner privilege posture and downgrade safety are tested. Owner/superuser remains outside runtime integrity assumptions. |
| Immutable asset canonical data | **Implemented and evidenced.** Direct canonical asset update/delete is rejected. |
| Posting idempotency conflict | **Implemented and evidenced in service checkpoint.** `post_in_transaction` requires caller transaction and uses a savepoint; it never commits/rolls back caller work. Standalone `post` owns a transaction only when none exists. Same canonical request returns same header; different payload is typed idempotency conflict; external source collision is a non-leaking typed source conflict. Real concurrent sessions are tested. |
| Asset vs custody separation | **Schema design implemented.** Global assets are separate from tenant accounts; provider mapping remains deliberately out of scope. |
| No fee/EVM policy changes | **Implemented.** No fee logic, backfill, EVM dual-write, route, worker, provider, or payout behavior was introduced. |

## Test isolation and database-role strategy

Ledger tests must not use the legacy shared `test_session` fixture: its owner-privileged `TRUNCATE ... CASCADE` reset can cross into ledger tables and is incompatible with ledger runtime guards. The numeric checkpoint uses `LEDGER_TEST_DATABASE_URL`, a separately prepared disposable ledger database, without table truncation or trigger disablement. Each test uses new rows and rolls back uncommitted test work.

For the later immutability/`TRUNCATE` substage, setup must be explicitly split: a privileged disposable-DB owner may create/drop the disposable database or schema before tests; a distinct non-owner runtime test role executes ledger DML and must have neither `TRUNCATE`, ownership, DDL, nor trigger-disable ability. Tests must not weaken any integrity guard to obtain green results. PostgreSQL owner/superuser bypass limits remain explicit.

## Numeric checkpoint run — 2026-09-05 (exact-bound correction)

The previous direct SQL test was corrected because its random UUID parents made SQLSTATE `23503` foreign-key failure a confound and the handwritten upper-bound literal was not auditable. `src/ledger/amounts.py` now derives `ATOMIC_AMOUNT_UPPER_BOUND_SQL` from `str(10**78)` once; migration and ORM import its one check expression. The direct asyncpg test retrieves `pg_get_constraintdef` and verifies the installed bound is exactly:

```text
amount_atomic < '<str(10**78)>'::numeric
```

Fresh dedicated disposable database `test_ledger_exact_bound` was migrated zero → `0010_ledger_foundation`, then:

```text
python -m pytest -q tests/test_ledger_foundation.py
3 passed in 0.32s
LEDGER_EXACT_BOUND_CHECKPOINT_EXIT=0
```

Evidence includes runtime acceptance of `10**77 - 1`, `10**77`, `10**77 + 1`, and `10**78 - 1`; runtime rejection of float/string/bool/fractional/nonfinite/huge-exponent Decimal/`10**78`; digest/idempotency equivalence for `100`, `Decimal('100.0')`, and `Decimal('1E2')` under default and low Decimal context; direct PostgreSQL rejection of `1.1`, `NaN`, positive/negative infinity, and exactly `10**78` by SQLSTATE `23514` / `ck_ledger_entry_atomic`; and direct positive insertion of a balanced posted two-entry transaction at `10**78 - 1` with deferred constraints forced immediate.

Database integrity stage is complete; its service-composition portion is recorded below.

## Immutable database checkpoint — 2026-09-05

`0010_ledger_foundation` is now self-contained: it freezes `_ATOMIC_AMOUNT_UPPER_BOUND = 10**78` and derives its local check SQL inside the migration; it no longer imports mutable `src.ledger` runtime code. Runtime/ORM continues using the shared runtime constant. The numeric test asserts `pg_get_constraintdef` equals the generated runtime semantics after migration.

Fresh dedicated disposable database `test_ledger_db_integrity` was migrated zero → `0010_ledger_foundation`, then the preserved numeric tests plus new isolated immutable-database tests ran with owner setup and separate runtime role:

```text
6 passed in 0.68s
LEDGER_IMMUTABLE_DB_EXIT=0
```

The test set covers exact numeric semantics; `posted_at` status consistency; rejection of committing `open` headers; valid-parent composite tenant FK failures in both directions; precise global source uniqueness; unbalanced and cross-asset-only netting rejection; independent per-asset balance success; immutable accounts/assets/headers/entries; append serialization behind `FOR UPDATE`; owner trigger-based truncate rejection; non-owner SQLSTATE `42501` rejection for `TRUNCATE` and trigger disablement. No trigger was disabled during setup or testing, and `tests/conftest.py` remains untouched.

Downgrade policy was also checked on disposable databases: empty `0010` downgrade to `0009_merchant_rails` exited `0`; a database containing posted journal rows raised `RuntimeError: refusing destructive ledger downgrade while posted journal rows exist` (exit `1`). This is intentional financial-history safety, not a production migration action.

## Posting service composition checkpoint — 2026-09-05

`LedgerPostingService` now exposes `post_in_transaction(...)`, which refuses absent caller transactions and uses `begin_nested()` only for its own work. It explicitly flushes entries before its permitted header `open`→`posted` transition. It does not commit or roll back caller scope. `post(...)` is a standalone wrapper only and refuses active callers, requiring explicit delegation. SQLAlchemy savepoint pre-flush behavior is documented: callers with pending unrelated writes should flush deliberately if ordering matters.

On collision, asyncpg constraint metadata is obtained through the safe `orig`/cause/context chain rather than assuming `exc.orig.constraint_name`. For either expected unique collision, the service first reads only `(caller merchant, idempotency key)`: same digest returns that row, a different digest raises `LedgerIdempotencyConflict`, and only absent own-key + exact global source unique yields non-leaking `LedgerSourceConflict`. Other integrity errors remain unmasked.

Fresh isolated database `test_ledger_service` was migrated zero → `0010_ledger_foundation` and ran:

```text
python -m pytest -q tests/test_ledger_posting_service.py
5 passed in 0.66s
LEDGER_SERVICE_CHECKPOINT_EXIT=0
```

Evidence covers active transaction requirement; standalone repeat/session usability; caller rollback of an unrelated row plus no journal residue; caller usability after handled idempotency conflict with a pending unrelated row; unrelated FK SQLSTATE `23503` remains unmasked; strict identity/direction validation; concurrent same-key same-payload (one header/two entries/same ID); concurrent same-key differing payload (one success/one typed conflict); cross-merchant same-source conflict (one success/one typed non-leaking conflict); and exact ledger row counts.

No payment lifecycle, invoice/outbox integration, EVM dual-write, fee, provider, or feature work is included.

## Final combined focused regression — 2026-09-05

The earlier claim that `SET search_path = pg_catalog, public` alone protected unqualified relations was incorrect: PostgreSQL implicitly searches a temporary schema first for relation names. The correction is implemented and evidenced: every relation reference inside ledger trigger functions is `public.ledger_transactions` or `public.ledger_entries`, and trigger functions use `SET search_path = pg_catalog, pg_temp`. Trigger function names and downgrade query are also explicitly `public` qualified. A runtime-role test with `TEMP` privilege creates shadow temp `ledger_transactions` and `ledger_entries` with plausible rows, then attempts to post an empty actual `public` header; the deferred actual public guard still rejects it as `balanced entries`.

Runtime ORM models remain schema-unspecified (`__table_args__` has no schema), so application database connections must retain their deployment-controlled default `search_path` resolving application tables to `public`; the hardened trigger internals do not rely on that caller setting. Non-owner runtime role privileges, owner/superuser limits, and loopback/`test_*` setup guard remain documented.

Fresh disposable `test_ledger_complete` migrated zero → `0010_ledger_foundation` ran all retained focused suites:

```text
12 passed in 1.38s
LEDGER_COMPLETE_FOCUSED_EXIT=0
```

Separate disposable lifecycle evidence after the same hardened migration:

```text
EMPTY_DOWNGRADE_EXIT=0
POSTED_DOWNGRADE_EXIT=1
```

The `0010` migration therefore upgrades from `0009`, permits destructive downgrade only while empty, and refuses downgrade after a posted journal. No production migration has run.

### Full-suite gate strategy

The future full safe suite must keep legacy tests on a fresh separate `0009` disposable database using their existing guarded reset fixture, and run ledger focused tests on a separate `0010` disposable database with the owner/runtime-role fixtures. Do not point the legacy fixture at `0010`: its `TRUNCATE ... CASCADE` reset would encounter ledger anti-truncate guards and must not be bypassed. Real-value `tests/test_e2e_chains.py` remains excluded.

## `0010` tables

### `ledger_assets`

Global canonical identity:

```text
id UUID
network_kind: evm|solana|ton|custodial
network_identifier: canonical chain/provider network
canonical_identifier: lower EVM contract / mint / jetton master / provider code
is_native bool
atomic_decimals int
symbol display-only
UNIQUE(network_kind, network_identifier, canonical_identifier, is_native)
```

No merchant ownership is included here. A custody/provider account is not an asset.

### `ledger_accounts`

Explicit tenant-owned accounting dimension:

```text
id UUID, merchant_id UUID
account_type: gateway_treasury_pending|merchant_payable|merchant_custodial_receivable|
              suspense_unmatched|refund_liability|merchant_fee_revenue|
              provider_fee_expense|network_fee_expense|provider_expense
custody_type: gateway_managed|merchant_custodial|merchant_liability|system
UNIQUE(merchant_id,id)
FOREIGN KEY merchant_id -> merchants ON DELETE RESTRICT
```

No arbitrary account code is accepted. Future provider-account mapping is separate; this slice does not mutate rails.

### `ledger_transactions`

```text
id UUID, merchant_id UUID
status open|posted (open is transient inside a DB transaction only)
source_namespace NOT NULL, source_external_id NOT NULL, source_digest SHA-256 hex NOT NULL
idempotency_key NOT NULL, posting_digest SHA-256 hex NOT NULL
posted_at
UNIQUE(merchant_id,id)
UNIQUE(merchant_id,idempotency_key)
UNIQUE(source_namespace,source_external_id)
FOREIGN KEY merchant_id -> merchants ON DELETE RESTRICT
```

The source global uniqueness avoids crediting one externally scoped event twice even when a provider account is accidentally shared by rails/merchants. For future EVM facts, namespace is e.g. chain identity and external ID is transaction hash plus log index; a dedicated source table will make this richer and immutable.

### `ledger_entries`

```text
id UUID, merchant_id UUID, ledger_transaction_id UUID, ledger_account_id UUID,
ledger_asset_id UUID, direction debit|credit, amount_atomic NUMERIC(78,0)>0
FOREIGN KEY (merchant_id,ledger_transaction_id) -> ledger_transactions(merchant_id,id)
FOREIGN KEY (merchant_id,ledger_account_id) -> ledger_accounts(merchant_id,id)
FOREIGN KEY ledger_asset_id -> ledger_assets ON DELETE RESTRICT
```

The journal can contain several assets only if each asset independently balances; no cross-asset netting.

## Atomic service contract

`LedgerPostingService.post(...)` receives merchant ID, source identity/digest, idempotency key, canonical posting content, and at least two typed lines. It:

1. validates every input before SQL;
2. opens transaction/savepoint;
3. checks existing merchant/key `FOR UPDATE` where practical;
4. returns existing header for same digest, raises conflict for different digest;
5. inserts header `open`, entries, then marks header `posted` in one commit;
6. PostgreSQL deferred balance trigger accepts only final balanced/posted state;
7. any error rolls back header and entries together.

No invoice, payment session, current EVM scanner, outbox, webhook, fee, or payout call enters this service yet.

## Direct SQL negative acceptance tests

On zero→head and upgrade-from-`0009` isolated PostgreSQL:

* direct unbalanced transaction rejected at deferred constraint commit;
* empty/open header rejected at commit;
* fewer than two lines rejected;
* cross-merchant transaction/account composite FK rejected;
* duplicate global source identity rejected;
* duplicate idempotency same merchant/different digest rejected by service conflict;
* concurrent same valid idempotency returns same header; different payload gets conflict;
* direct append/update/delete posted entry/header rejected;
* direct asset canonical/decimal update/delete rejected;
* direct truncate rejected for runtime test role;
* service exception causes atomic rollback with no header/entry residue.

## Privilege and migration note

The migration creates integrity triggers, but PostgreSQL owner/superuser may alter/disable/drop them. Deployment must use a non-owner runtime role that has only DML needed by posting, no DDL, no trigger disabling, no `TRUNCATE`, and no ownership of ledger tables. This slice cannot establish external managed-role policy; it documents and tests the trigger behavior under the available isolated runtime connection.

## Director technical decisions already fixed

* Separate immutable fact and decision tables later; no mutable verification state.
* Global asset registry, tenant account/custody dimension, composite FKs, atomic amounts, deferred per-asset balance, feature remains unused by runtime.
* `0010` is additive only.

## Product/finance decisions still intentionally open

* entitlement/disposition of overpayment excess;
* fee schedule and fee charging point; no default/retroactive 3%;
* refund/chargeback eligibility and recipient identity;
* settlement timing and operational reconciliation ownership.

Provider statement cadence, future asset registry governance, and non-EVM source finality are implementation/reliability tasks for the gateway team after their specific provider/network contracts are approved; they are not user-facing finance-policy questions in this slice.
