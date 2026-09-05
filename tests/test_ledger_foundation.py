"""Numeric checkpoint for the disabled ledger foundation.

The fixture is deliberately independent of shared ``test_session``: the latter
uses a legacy privileged TRUNCATE reset and must never touch ledger tables once
ledger integrity guards exist.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from decimal import Decimal, localcontext

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.db.models.ledger import LedgerAccount, LedgerAsset, LedgerDirection
from src.db.models.merchant import Merchant
from src.ledger.amounts import ATOMIC_AMOUNT_CHECK_SQL, ATOMIC_AMOUNT_UPPER_BOUND
from src.services.ledger_posting_service import (
    LedgerLine,
    LedgerPostingService,
    LedgerValidationError,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest_asyncio.fixture
async def ledger_session() -> AsyncSession:
    """Use only a separately prepared disposable ledger DB; rollback per test."""
    database_url = os.getenv("LEDGER_TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("LEDGER_TEST_DATABASE_URL must point to dedicated ledger database")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await outer_transaction.rollback()
    finally:
        await engine.dispose()


async def seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    merchant = Merchant(name="ledger numeric merchant", email=f"ledger-{uuid.uuid4()}@test.invalid")
    asset = LedgerAsset(
        network_kind="evm",
        network_identifier="8453",
        canonical_identifier=f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        is_native=False,
        atomic_decimals=6,
        symbol="USDC",
    )
    session.add_all([merchant, asset])
    await session.flush()
    debit = LedgerAccount(
        merchant_id=merchant.id,
        account_type="gateway_treasury_pending",
        custody_type="gateway_managed",
    )
    credit = LedgerAccount(
        merchant_id=merchant.id, account_type="merchant_payable", custody_type="merchant_liability"
    )
    session.add_all([debit, credit])
    await session.commit()
    return merchant.id, asset.id, debit.id, credit.id


def lines(asset: uuid.UUID, debit: uuid.UUID, credit: uuid.UUID, amount: int | Decimal):
    return (
        LedgerLine(debit, asset, LedgerDirection.DEBIT, amount),
        LedgerLine(credit, asset, LedgerDirection.CREDIT, amount),
    )


@pytest.mark.asyncio
async def test_atomic_boundary_accepts_exact_range_and_rejects_coercions(
    ledger_session: AsyncSession,
):
    merchant, asset, debit, credit = await seed(ledger_session)
    service = LedgerPostingService(ledger_session)
    for value in (10**77 - 1, 10**77, 10**77 + 1, ATOMIC_AMOUNT_UPPER_BOUND - 1):
        posted = await service.post(
            merchant_id=merchant,
            source_namespace="numeric-boundary",
            source_external_id=f"source-{value}",
            source_digest=sha(f"source-{value}"),
            idempotency_key=f"key-{value}",
            lines=lines(asset, debit, credit, value),
        )
        assert posted.id

    for invalid in (
        True,
        1.0,
        "1",
        Decimal("1.1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("1E100"),
        Decimal("1E+999999999"),
        ATOMIC_AMOUNT_UPPER_BOUND,
    ):
        with pytest.raises(LedgerValidationError):
            await service.post(
                merchant_id=merchant,
                source_namespace="numeric-invalid",
                source_external_id=f"source-{invalid}",
                source_digest=sha(f"source-{invalid}"),
                idempotency_key=f"key-{invalid}",
                lines=lines(asset, debit, credit, invalid),
            )


@pytest.mark.asyncio
async def test_equivalent_decimal_forms_have_identical_idempotency_digest_at_low_context(
    ledger_session: AsyncSession,
):
    merchant, asset, debit, credit = await seed(ledger_session)
    service = LedgerPostingService(ledger_session)
    first = await service.post(
        merchant_id=merchant,
        source_namespace="numeric-canonical",
        source_external_id="canonical-source",
        source_digest=sha("canonical-source"),
        idempotency_key="canonical-key",
        lines=lines(asset, debit, credit, 100),
    )
    for equivalent in (Decimal("100.0"), Decimal("1E2")):
        repeated = await service.post(
            merchant_id=merchant,
            source_namespace="numeric-canonical",
            source_external_id="canonical-source",
            source_digest=sha("canonical-source"),
            idempotency_key="canonical-key",
            lines=lines(asset, debit, credit, equivalent),
        )
        assert repeated.id == first.id
    with localcontext() as context:
        context.prec = 5
        repeated = await service.post(
            merchant_id=merchant,
            source_namespace="numeric-canonical",
            source_external_id="canonical-source",
            source_digest=sha("canonical-source"),
            idempotency_key="canonical-key",
            lines=lines(asset, debit, credit, Decimal("1E2")),
        )
    assert repeated.id == first.id


async def _insert_direct_parents(
    connection: asyncpg.Connection,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    merchant, asset, debit, credit = (uuid.uuid4() for _ in range(4))
    await connection.execute(
        "INSERT INTO merchants (id,name,email,is_active) VALUES ($1,$2,$3,true)",
        merchant,
        "numeric direct sql merchant",
        f"numeric-direct-{merchant}@test.invalid",
    )
    await connection.execute(
        "INSERT INTO ledger_assets (id,network_kind,network_identifier,canonical_identifier,is_native,atomic_decimals,symbol) VALUES ($1,'evm','8453',$2,false,6,'USDC')",
        asset,
        f"0x{asset.hex}{uuid.uuid4().hex[:8]}",
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


async def _insert_open_header(
    connection: asyncpg.Connection, merchant: uuid.UUID, label: str
) -> uuid.UUID:
    transaction_id = uuid.uuid4()
    await connection.execute(
        "INSERT INTO ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest) VALUES ($1,$2,'open','numeric-direct',$3,$4,$5,$6)",
        transaction_id,
        merchant,
        f"source-{label}-{transaction_id}",
        sha(f"source-{label}-{transaction_id}"),
        f"key-{label}-{transaction_id}",
        sha(f"posting-{label}-{transaction_id}"),
    )
    return transaction_id


@pytest.mark.asyncio
async def test_direct_sql_exact_bound_and_numeric_checks_with_valid_parents():
    database_url = os.environ["LEDGER_TEST_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    connection = await asyncpg.connect(database_url)
    outer = connection.transaction()
    await outer.start()
    try:
        definition = await connection.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_ledger_entry_atomic'"
        )
        expected_definition = (
            "CHECK ((((amount_atomic)::text <> 'NaN'::text) "
            "AND (amount_atomic > (0)::numeric) "
            f"AND (amount_atomic < '{ATOMIC_AMOUNT_UPPER_BOUND}'::numeric) "
            "AND (amount_atomic = trunc(amount_atomic))))"
        )
        assert definition == expected_definition
        assert "CAST(amount_atomic AS TEXT) <> 'NaN'" in ATOMIC_AMOUNT_CHECK_SQL
        merchant, asset, debit, credit = await _insert_direct_parents(connection)

        for label, amount_sql in (
            ("fractional", "1.1"),
            ("nan", "'NaN'::numeric"),
            ("positive-infinity", "'Infinity'::numeric"),
            ("negative-infinity", "'-Infinity'::numeric"),
            ("out-of-range", str(ATOMIC_AMOUNT_UPPER_BOUND)),
        ):
            savepoint = connection.transaction()
            await savepoint.start()
            try:
                transaction_id = await _insert_open_header(connection, merchant, label)
                with pytest.raises(asyncpg.CheckViolationError) as exc:
                    await connection.execute(
                        "INSERT INTO ledger_entries (id,merchant_id,ledger_transaction_id,ledger_account_id,ledger_asset_id,direction,amount_atomic) "
                        f"VALUES ('{uuid.uuid4()}','{merchant}','{transaction_id}','{debit}','{asset}','debit',{amount_sql})"
                    )
                assert exc.value.sqlstate == "23514"
                assert exc.value.constraint_name == "ck_ledger_entry_atomic"
            finally:
                await savepoint.rollback()

        positive_header = await _insert_open_header(connection, merchant, "positive-boundary")
        maximum = ATOMIC_AMOUNT_UPPER_BOUND - 1
        for account, direction in ((debit, "debit"), (credit, "credit")):
            await connection.execute(
                "INSERT INTO ledger_entries (id,merchant_id,ledger_transaction_id,ledger_account_id,ledger_asset_id,direction,amount_atomic) "
                f"VALUES ('{uuid.uuid4()}','{merchant}','{positive_header}','{account}','{asset}','{direction}',{maximum})"
            )
        await connection.execute(
            "UPDATE ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
            positive_header,
        )
        await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    finally:
        await outer.rollback()
        await connection.close()
