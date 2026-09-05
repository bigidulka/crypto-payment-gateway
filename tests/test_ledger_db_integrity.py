"""Immutable database integrity checkpoint for local ledger `0010`.

This module uses a dedicated disposable database. It never uses the legacy
shared truncation fixture, and never disables ledger triggers.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from urllib.parse import urlsplit

import asyncpg
import pytest


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def owner_url() -> str:
    url = os.getenv("LEDGER_OWNER_DATABASE_URL")
    if not url:
        pytest.fail("LEDGER_OWNER_DATABASE_URL must point to dedicated ledger database")
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "::1"} or not parsed.path.removeprefix("/").startswith(
        "test_"
    ):
        pytest.fail("LEDGER_OWNER_DATABASE_URL must use loopback and a test_* database")
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def connect_owner() -> asyncpg.Connection:
    return await asyncpg.connect(owner_url())


async def seed_merchant(connection: asyncpg.Connection, label: str):
    merchant, asset, debit, credit = (uuid.uuid4() for _ in range(4))
    await connection.execute(
        "INSERT INTO merchants (id,name,email,is_active) VALUES ($1,$2,$3,true)",
        merchant,
        f"ledger {label}",
        f"ledger-{label}-{merchant}@test.invalid",
    )
    await connection.execute(
        "INSERT INTO ledger_assets (id,network_kind,network_identifier,canonical_identifier,is_native,atomic_decimals,symbol) VALUES ($1,'evm','8453',$2,false,6,'USDC')",
        asset,
        f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
    )
    for account, account_type, custody_type in (
        (debit, "gateway_treasury_pending", "gateway_managed"),
        (credit, "merchant_payable", "merchant_liability"),
    ):
        await connection.execute(
            "INSERT INTO ledger_accounts (id,merchant_id,account_type,custody_type) VALUES ($1,$2,$3,$4)",
            account,
            merchant,
            account_type,
            custody_type,
        )
    return merchant, asset, debit, credit


async def open_header(connection: asyncpg.Connection, merchant: uuid.UUID, label: str) -> uuid.UUID:
    transaction_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest) VALUES ($1,$2,'open','ledger-db-test',$3,$4,$5,$6)",
        transaction_id,
        merchant,
        f"source-{label}-{transaction_id}",
        sha(f"source-{label}-{transaction_id}"),
        f"idempotency-{label}-{transaction_id}",
        sha(f"posting-{label}-{transaction_id}"),
    )
    return transaction_id


async def insert_entry(connection, merchant, transaction_id, account, asset, direction, amount):
    entry_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO ledger_entries (id,merchant_id,ledger_transaction_id,ledger_account_id,ledger_asset_id,direction,amount_atomic) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        entry_id,
        merchant,
        transaction_id,
        account,
        asset,
        direction,
        amount,
    )
    return entry_id


async def post_balanced(connection, merchant, debit, credit, asset_amounts, label):
    transaction_id = await open_header(connection, merchant, label)
    entry_ids = []
    for asset, amount in asset_amounts:
        entry_ids.append(
            await insert_entry(connection, merchant, transaction_id, debit, asset, "debit", amount)
        )
        entry_ids.append(
            await insert_entry(
                connection, merchant, transaction_id, credit, asset, "credit", amount
            )
        )
    await connection.execute(
        "UPDATE ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
        transaction_id,
    )
    await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return transaction_id, entry_ids


async def expect_rolled_back_error(connection, expected, operation):
    savepoint = connection.transaction()
    await savepoint.start()
    try:
        with pytest.raises(expected) as exc:
            await operation()
        return exc.value
    finally:
        await savepoint.rollback()


@pytest.mark.asyncio
async def test_final_state_tenant_balance_source_and_immutability_guards():
    connection = await connect_owner()
    outer = connection.transaction()
    await outer.start()
    try:
        merchant_a, asset_a, debit_a, credit_a = await seed_merchant(connection, "a")
        merchant_b, asset_b, debit_b, credit_b = await seed_merchant(connection, "b")

        error = await expect_rolled_back_error(
            connection,
            asyncpg.CheckViolationError,
            lambda: connection.execute(
                "INSERT INTO ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest,posted_at) VALUES ($1,$2,'posted','header-check',$3,$4,$5,$6,NULL)",
                uuid.uuid4(),
                merchant_a,
                str(uuid.uuid4()),
                sha("posted-at-source"),
                str(uuid.uuid4()),
                sha("posted-at-posting"),
            ),
        )
        assert error.constraint_name == "ck_ledger_transaction_posted_at"

        async def empty_posted():
            header = await open_header(connection, merchant_a, "empty-posted")
            await connection.execute(
                "UPDATE ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
                header,
            )
            await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

        error = await expect_rolled_back_error(connection, asyncpg.RaiseError, empty_posted)
        assert "balanced entries" in str(error)
        await connection.execute("SAVEPOINT open_header_test")
        await open_header(connection, merchant_a, "open-must-not-commit")
        with pytest.raises(asyncpg.RaiseError) as open_exc:
            await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        assert "must be posted before commit" in str(open_exc.value)
        await connection.execute("ROLLBACK TO SAVEPOINT open_header_test")

        async def duplicate_source():
            first = uuid.uuid4()
            source = str(uuid.uuid4())
            await connection.execute(
                "INSERT INTO ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest) VALUES ($1,$2,'open','source-unique',$3,$4,$5,$6)",
                first,
                merchant_a,
                source,
                sha("source-one"),
                str(uuid.uuid4()),
                sha("posting-one"),
            )
            await connection.execute(
                "INSERT INTO ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest) VALUES ($1,$2,'open','source-unique',$3,$4,$5,$6)",
                uuid.uuid4(),
                merchant_b,
                source,
                sha("source-two"),
                str(uuid.uuid4()),
                sha("posting-two"),
            )

        error = await expect_rolled_back_error(
            connection, asyncpg.UniqueViolationError, duplicate_source
        )
        assert error.sqlstate == "23505" and error.constraint_name == "uq_ledger_transaction_source"

        async def foreign_account():
            header = await open_header(connection, merchant_a, "tenant-account")
            await insert_entry(connection, merchant_a, header, credit_b, asset_a, "debit", 1)

        error = await expect_rolled_back_error(
            connection, asyncpg.ForeignKeyViolationError, foreign_account
        )
        assert error.constraint_name == "ledger_entries_merchant_id_ledger_account_id_fkey"

        async def foreign_transaction():
            header = await open_header(connection, merchant_b, "tenant-transaction")
            await insert_entry(connection, merchant_a, header, credit_a, asset_a, "debit", 1)

        error = await expect_rolled_back_error(
            connection, asyncpg.ForeignKeyViolationError, foreign_transaction
        )
        assert error.constraint_name == "ledger_entries_merchant_id_ledger_transaction_id_fkey"

        async def unbalanced():
            header = await open_header(connection, merchant_a, "unbalanced")
            await insert_entry(connection, merchant_a, header, debit_a, asset_a, "debit", 10)
            await insert_entry(connection, merchant_a, header, credit_a, asset_a, "credit", 9)
            await connection.execute(
                "UPDATE ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
                header,
            )
            await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

        error = await expect_rolled_back_error(connection, asyncpg.RaiseError, unbalanced)
        assert "balanced entries" in str(error)

        async def cross_asset():
            header = await open_header(connection, merchant_a, "cross-asset")
            await insert_entry(connection, merchant_a, header, debit_a, asset_a, "debit", 10)
            await insert_entry(connection, merchant_a, header, credit_a, asset_b, "credit", 10)
            await connection.execute(
                "UPDATE ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
                header,
            )
            await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

        error = await expect_rolled_back_error(connection, asyncpg.RaiseError, cross_asset)
        assert "balanced entries" in str(error)

        independent_asset = await connection.fetchval(
            "INSERT INTO ledger_assets (id,network_kind,network_identifier,canonical_identifier,is_native,atomic_decimals,symbol) VALUES ($1,'evm','8453',$2,false,6,'USDT') RETURNING id",
            uuid.uuid4(),
            f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        )
        posted, entry_ids = await post_balanced(
            connection,
            merchant_a,
            debit_a,
            credit_a,
            [(asset_a, 10), (independent_asset, 20)],
            "two-assets",
        )

        for operation in (
            lambda: connection.execute(
                "UPDATE ledger_accounts SET account_type='suspense_unmatched' WHERE id=$1", debit_a
            ),
            lambda: connection.execute("DELETE FROM ledger_accounts WHERE id=$1", debit_a),
            lambda: connection.execute(
                "UPDATE ledger_assets SET symbol='CHANGED' WHERE id=$1", asset_a
            ),
            lambda: connection.execute("DELETE FROM ledger_assets WHERE id=$1", asset_a),
            lambda: connection.execute(
                "UPDATE ledger_transactions SET source_namespace='changed' WHERE id=$1", posted
            ),
            lambda: connection.execute("DELETE FROM ledger_transactions WHERE id=$1", posted),
            lambda: connection.execute(
                "UPDATE ledger_entries SET amount_atomic=11 WHERE id=$1", entry_ids[0]
            ),
            lambda: connection.execute("DELETE FROM ledger_entries WHERE id=$1", entry_ids[0]),
            lambda: insert_entry(connection, merchant_a, posted, debit_a, asset_a, "debit", 1),
        ):
            error = await expect_rolled_back_error(connection, asyncpg.RaiseError, operation)
            assert error.sqlstate == "P0001"
    finally:
        await outer.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_posted_entry_append_serializes_on_header_lock_then_rejects():
    owner = await connect_owner()
    try:
        async with owner.transaction():
            merchant, asset, debit, credit = await seed_merchant(owner, "append-lock")
            posted, _ = await post_balanced(
                owner, merchant, debit, credit, [(asset, 5)], "append-lock"
            )
        contender = await connect_owner()
        try:
            lock_transaction = owner.transaction()
            await lock_transaction.start()
            await owner.execute("SELECT id FROM ledger_transactions WHERE id=$1 FOR UPDATE", posted)
            contender_transaction = contender.transaction()
            await contender_transaction.start()
            append_task = asyncio.create_task(
                insert_entry(contender, merchant, posted, debit, asset, "debit", 1)
            )
            await asyncio.sleep(0.05)
            assert not append_task.done()
            await lock_transaction.commit()
            with pytest.raises(asyncpg.RaiseError, match="immutable"):
                await append_task
            await contender_transaction.rollback()
        finally:
            await contender.close()
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_owner_truncate_guard_and_nonowner_runtime_role_cannot_truncate_or_disable_triggers():
    owner = await connect_owner()
    runtime_name = f"ledger_runtime_{uuid.uuid4().hex[:12]}"
    runtime_password = "ledger-runtime-test-password"
    runtime = None
    try:
        database_name = await owner.fetchval("SELECT current_database()")
        await owner.execute(
            f"CREATE ROLE \"{runtime_name}\" LOGIN PASSWORD '{runtime_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
        )
        await owner.execute(f'GRANT CONNECT ON DATABASE "{database_name}" TO "{runtime_name}"')
        await owner.execute(f'GRANT USAGE ON SCHEMA public TO "{runtime_name}"')
        await owner.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ledger_assets, ledger_accounts, ledger_transactions, ledger_entries TO "{runtime_name}"'
        )
        async with owner.transaction():
            merchant, asset, debit, credit = await seed_merchant(owner, "runtime-post")
        parsed = urlsplit(owner_url())
        runtime = await asyncpg.connect(
            host=parsed.hostname,
            port=parsed.port,
            database=parsed.path.removeprefix("/"),
            user=runtime_name,
            password=runtime_password,
        )
        async with runtime.transaction():
            posted, entry_ids = await post_balanced(
                runtime, merchant, debit, credit, [(asset, 7)], "runtime-post"
            )
        for operation in (
            lambda: runtime.execute(
                "UPDATE ledger_transactions SET source_namespace='changed' WHERE id=$1", posted
            ),
            lambda: runtime.execute("DELETE FROM ledger_entries WHERE id=$1", entry_ids[0]),
            lambda: insert_entry(runtime, merchant, posted, debit, asset, "debit", 1),
        ):
            error = await expect_rolled_back_error(runtime, asyncpg.RaiseError, operation)
            assert error.sqlstate == "P0001"
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as truncate_error:
            await runtime.execute("TRUNCATE ledger_entries")
        assert truncate_error.value.sqlstate == "42501"
        with pytest.raises(asyncpg.InsufficientPrivilegeError) as trigger_error:
            await runtime.execute("ALTER TABLE ledger_entries DISABLE TRIGGER ALL")
        assert trigger_error.value.sqlstate == "42501"
    finally:
        if runtime:
            await runtime.close()
        await owner.execute(f'DROP OWNED BY "{runtime_name}"')
        await owner.execute(f'DROP ROLE IF EXISTS "{runtime_name}"')
        await owner.close()


@pytest.mark.asyncio
async def test_runtime_temp_shadow_cannot_bypass_public_final_state_guard():
    owner = await connect_owner()
    runtime_name = f"ledger_temp_{uuid.uuid4().hex[:12]}"
    runtime_password = "ledger-temp-test-password"
    runtime = None
    try:
        database_name = await owner.fetchval("SELECT current_database()")
        await owner.execute(
            f"CREATE ROLE \"{runtime_name}\" LOGIN PASSWORD '{runtime_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
        )
        await owner.execute(
            f'GRANT CONNECT, TEMP ON DATABASE "{database_name}" TO "{runtime_name}"'
        )
        await owner.execute(f'GRANT USAGE ON SCHEMA public TO "{runtime_name}"')
        await owner.execute(
            f'GRANT SELECT, INSERT, UPDATE ON ledger_assets, ledger_accounts, ledger_transactions, ledger_entries TO "{runtime_name}"'
        )
        async with owner.transaction():
            merchant, asset, debit, credit = await seed_merchant(owner, "temp-shadow")
        parsed = urlsplit(owner_url())
        runtime = await asyncpg.connect(
            host=parsed.hostname,
            port=parsed.port,
            database=parsed.path.removeprefix("/"),
            user=runtime_name,
            password=runtime_password,
        )
        async with runtime.transaction():
            await runtime.execute("CREATE TEMP TABLE ledger_transactions (id uuid, status text)")
            await runtime.execute(
                "CREATE TEMP TABLE ledger_entries (ledger_transaction_id uuid, ledger_asset_id uuid, direction text, amount_atomic numeric)"
            )
            header = uuid.uuid4()
            fake_asset = uuid.uuid4()
            await runtime.execute("INSERT INTO ledger_transactions VALUES ($1, 'posted')", header)
            await runtime.execute(
                "INSERT INTO ledger_entries VALUES ($1, $2, 'debit', 10), ($1, $2, 'credit', 10)",
                header,
                fake_asset,
            )
            await runtime.execute(
                "INSERT INTO public.ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest) VALUES ($1,$2,'open','temp-shadow',$3,$4,$5,$6)",
                header,
                merchant,
                str(header),
                sha(f"shadow-source-{header}"),
                str(header),
                sha(f"shadow-posting-{header}"),
            )
            await runtime.execute(
                "UPDATE public.ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
                header,
            )
            with pytest.raises(asyncpg.RaiseError, match="balanced entries"):
                await runtime.execute("SET CONSTRAINTS ALL IMMEDIATE")
    finally:
        if runtime:
            await runtime.close()
        await owner.execute(f'DROP OWNED BY "{runtime_name}"')
        await owner.execute(f'DROP ROLE IF EXISTS "{runtime_name}"')
        await owner.close()
