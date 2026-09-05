# Ledger `0010` review findings and bounded correction plan

Date: 2026-09-05  
Status: director review — package **not accepted**. Local, uncommitted `0010` only; no commit, push, production action, rail call, fee, backfill, or feature enablement.

The prior ADR acceptance matrix overstated guarantees that were absent from the implementation and tests. It must distinguish **implemented and evidenced** from **pending** after each bounded substage.

## P0: amounts, database representation and idempotency canonicalization

1. `Decimal(value)` accepted floats and strings. The public posting boundary must accept only `int` or `Decimal`, excluding `bool`.
2. `len(str(Decimal('1E+100')))` is not a safe 78-digit bound; validate by finite/integral check and bounded integer conversion/comparison: `0 < n < 10**78`.
3. `100`, `Decimal('100.0')`, and `Decimal('1E2')` must have one canonical atomic integer representation and identical posting digest/idempotency semantics.
4. Per-asset balance must use Python `int` sums, not `Decimal` arithmetic subject to the default context precision of 28.
5. PostgreSQL `NUMERIC(78,0)` can round fractional values on coercion before the old integer check; the migration needs a database representation/check strategy that genuinely rejects direct-SQL fractional/non-finite/out-of-range input, and the ADR must explain it.
6. Evidence must cover `10**77 ± 1`, `10**78 - 1`, `10**78`, `Decimal('1E100')`, equivalent representations `100`/`100.0`/`1E2`, and float rejection.

## P0: immutability and database privilege posture (substage 2; not changed in substage 1)

1. `ledger_accounts.account_type` and `custody_type` are mutable and can reinterpret historical entries. Make account identities/types/owner fully immutable or prohibit mutation once referenced.
2. `TRUNCATE` promise is unimplemented. Add statement-level guard and test it under a dedicated non-owner runtime role. State superuser/owner limitations precisely.
3. Open-header deletion currently returns `NEW` in a DELETE trigger (which is `NULL`) and silently suppresses delete. Either reject it or return `OLD` explicitly for documented allowed deletion.
4. A posted header requires a non-null `posted_at`; enforce it.
5. Prove direct append to posted entries is rejected, including appropriate concurrency behavior.
6. Test an unposted/open header cannot commit.

## P0: test accuracy and missing acceptance coverage (substage 2; not changed in substage 1)

1. The earlier cross-tenant test used a random nonexistent transaction and therefore only proved a simple foreign key. Seed valid parent rows and prove both foreign-account and foreign-transaction tenant violations.
2. The source-duplicate test must avoid a confounding unbalanced/open-header error and assert the unique constraint/SQLSTATE.
3. Add direct tests for: empty/open header; unbalanced two-or-more lines; two independently balanced assets accepted; cross-asset-only netting rejected; direct updates/deletes of account/header/entry/asset; append after post; and runtime-role `TRUNCATE` denial.
4. Add concurrent same-idempotency same-payload and different-payload tests, plus atomic rollback/no-residue.
5. Assert SQLSTATE/constraint name where PostgreSQL provides it instead of broad exception matching.

## P0: transaction composition (substage 2; not changed in substage 1)

1. `LedgerPostingService.post()` currently owns `session.begin()`, preventing later atomic composition with invoice/outbox work.
2. Replace it with `post_in_transaction()` for a caller-owned transaction and savepoint-scoped unique handling. A separate standalone wrapper may own a transaction.
3. It must never commit or rollback caller work. Test rollback of an unrelated caller row, session usability after idempotency conflict, and a same-source/different-merchant/key collision that does not leak the other merchant's transaction.

## Bounded execution order

### Substage 1 — now

Amounts only: strict runtime atomic normalization, canonical digest, integer balance arithmetic, safe PostgreSQL amount representation/constraints, focused PostgreSQL evidence, revised ADR implemented-vs-pending status. No immutability or transaction-composition changes in this substage.

### Substage 2 — only after director reads checkpoint

Immutability, runtime-role truncate defense, transaction composition, concurrency, and the remaining acceptance matrix tests.
