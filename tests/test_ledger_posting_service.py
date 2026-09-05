"""Caller-transaction and concurrency checkpoint for disabled ledger posting."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models.ledger import (
    LedgerAccount,
    LedgerAsset,
    LedgerDirection,
    LedgerEntry,
    LedgerTransaction,
)
from src.db.models.merchant import Merchant
from src.services.ledger_posting_service import (
    LedgerIdempotencyConflict,
    LedgerLine,
    LedgerPostingService,
    LedgerSourceConflict,
    LedgerValidationError,
)


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def service_url() -> str:
    url = os.getenv("LEDGER_SERVICE_DATABASE_URL")
    if not url:
        pytest.fail("LEDGER_SERVICE_DATABASE_URL must point to a dedicated database")
    return url


@pytest_asyncio.fixture
async def service_factory():
    engine = create_async_engine(service_url(), pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed(session: AsyncSession, label: str):
    merchant = Merchant(
        name=f"service {label}", email=f"service-{label}-{uuid.uuid4()}@test.invalid"
    )
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
    await session.flush()
    return merchant.id, asset.id, debit.id, credit.id


def lines(asset: uuid.UUID, debit: uuid.UUID, credit: uuid.UUID, amount: int = 100):
    return (
        LedgerLine(debit, asset, LedgerDirection.DEBIT, amount),
        LedgerLine(credit, asset, LedgerDirection.CREDIT, amount),
    )


def request(merchant, asset, debit, credit, *, key="key", source="source", amount=100):
    return {
        "merchant_id": merchant,
        "source_namespace": "service-test",
        "source_external_id": source,
        "source_digest": sha(f"digest-{source}"),
        "idempotency_key": key,
        "lines": lines(asset, debit, credit, amount),
    }


async def counts(session: AsyncSession) -> tuple[int, int]:
    return (
        await session.scalar(select(func.count()).select_from(LedgerTransaction)),
        await session.scalar(select(func.count()).select_from(LedgerEntry)),
    )


@pytest.mark.asyncio
async def test_post_in_transaction_requires_caller_and_standalone_is_reusable(service_factory):
    async with service_factory() as session:
        merchant, asset, debit, credit = await seed(session, "standalone")
        await session.commit()
        service = LedgerPostingService(session)
        baseline = await counts(session)
        await session.rollback()
        with pytest.raises(LedgerValidationError, match="requires an active"):
            await service.post_in_transaction(**request(merchant, asset, debit, credit))
        first = await service.post(
            **request(merchant, asset, debit, credit, key="standalone", source="standalone")
        )
        second = await service.post(
            **request(merchant, asset, debit, credit, key="standalone", source="standalone")
        )
        assert first.id == second.id
        assert await counts(session) == (baseline[0] + 1, baseline[1] + 2)
        await session.rollback()
        async with session.begin():
            with pytest.raises(LedgerValidationError, match="use post_in_transaction"):
                await service.post(
                    **request(
                        merchant,
                        asset,
                        debit,
                        credit,
                        key="nested-wrapper",
                        source="nested-wrapper",
                    )
                )


@pytest.mark.asyncio
async def test_caller_rollback_no_residue_and_handled_conflict_keeps_pending_caller_work(
    service_factory,
):
    async with service_factory() as session:
        merchant, asset, debit, credit = await seed(session, "caller")
        await session.commit()
        service = LedgerPostingService(session)
        baseline = await counts(session)
        await session.rollback()
        with pytest.raises(RuntimeError, match="abort caller"):
            async with session.begin():
                session.add(
                    Merchant(
                        name="unrelated rollback", email=f"rollback-{uuid.uuid4()}@test.invalid"
                    )
                )
                await service.post_in_transaction(
                    **request(merchant, asset, debit, credit, key="rollback", source="rollback")
                )
                raise RuntimeError("abort caller")
        assert await counts(session) == baseline
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Merchant)
                .where(Merchant.name == "unrelated rollback")
            )
            == 0
        )
        await session.rollback()

        async with session.begin():
            first = await service.post_in_transaction(
                **request(
                    merchant, asset, debit, credit, key="caller-conflict", source="caller-conflict"
                )
            )
            session.add(
                Merchant(
                    name="pending before savepoint", email=f"pending-{uuid.uuid4()}@test.invalid"
                )
            )
            with pytest.raises(LedgerIdempotencyConflict):
                await service.post_in_transaction(
                    **request(
                        merchant,
                        asset,
                        debit,
                        credit,
                        key="caller-conflict",
                        source="caller-conflict-different",
                        amount=101,
                    )
                )
            session.add(
                Merchant(name="unrelated committed", email=f"committed-{uuid.uuid4()}@test.invalid")
            )
        assert first.id
        assert await counts(session) == (baseline[0] + 1, baseline[1] + 2)
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Merchant)
                .where(Merchant.name == "unrelated committed")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_masked_and_savepoint_leaves_no_ledger_residue(
    service_factory,
):
    async with service_factory() as session:
        merchant, asset, debit, credit = await seed(session, "integrity")
        await session.commit()
        baseline = await counts(session)
        await session.rollback()
        service = LedgerPostingService(session)
        async with session.begin():
            bad_asset = uuid.uuid4()
            bad = (
                LedgerLine(debit, bad_asset, LedgerDirection.DEBIT, 100),
                LedgerLine(credit, bad_asset, LedgerDirection.CREDIT, 100),
            )
            with pytest.raises(IntegrityError) as exc:
                await service.post_in_transaction(
                    **{
                        **request(merchant, asset, debit, credit, key="bad-fk", source="bad-fk"),
                        "lines": bad,
                    }
                )
            assert getattr(exc.value.orig, "sqlstate", None) == "23503"
            assert "ledger_entries_ledger_asset_id_fkey" in str(exc.value)
            session.add(
                Merchant(name="caller still usable", email=f"usable-{uuid.uuid4()}@test.invalid")
            )
        assert await counts(session) == baseline


async def concurrent_post(factory, payload, gate: asyncio.Event):
    async with factory() as session:
        await gate.wait()
        return await LedgerPostingService(session).post(**payload)


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_and_source_conflicts_are_typed_and_tenant_safe(
    service_factory,
):
    async with service_factory() as setup:
        merchant_a, asset_a, debit_a, credit_a = await seed(setup, "concurrency-a")
        merchant_b, asset_b, debit_b, credit_b = await seed(setup, "concurrency-b")
        await setup.commit()
        baseline = await counts(setup)

    gate = asyncio.Event()
    identical = request(merchant_a, asset_a, debit_a, credit_a, key="same", source="same")
    tasks = [
        asyncio.create_task(concurrent_post(service_factory, identical, gate)) for _ in range(2)
    ]
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results[0].id == results[1].id

    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(
            concurrent_post(
                service_factory,
                request(
                    merchant_a,
                    asset_a,
                    debit_a,
                    credit_a,
                    key="different",
                    source="different",
                    amount=100,
                ),
                gate,
            )
        ),
        asyncio.create_task(
            concurrent_post(
                service_factory,
                request(
                    merchant_a,
                    asset_a,
                    debit_a,
                    credit_a,
                    key="different",
                    source="different-other",
                    amount=101,
                ),
                gate,
            )
        ),
    ]
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, LedgerIdempotencyConflict) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1

    gate = asyncio.Event()
    source = "shared-external-coordinate"
    tasks = [
        asyncio.create_task(
            concurrent_post(
                service_factory,
                request(merchant_a, asset_a, debit_a, credit_a, key="source-a", source=source),
                gate,
            )
        ),
        asyncio.create_task(
            concurrent_post(
                service_factory,
                request(merchant_b, asset_b, debit_b, credit_b, key="source-b", source=source),
                gate,
            )
        ),
    ]
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, LedgerSourceConflict) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    source_error = next(result for result in results if isinstance(result, LedgerSourceConflict))
    assert (
        "merchant" not in str(source_error).lower()
        and "transaction" not in str(source_error).lower()
    )

    async with service_factory() as verify:
        assert await counts(verify) == (baseline[0] + 3, baseline[1] + 6)


@pytest.mark.asyncio
async def test_rejects_bad_identity_and_direction_types_before_database_work(service_factory):
    async with service_factory() as session:
        merchant, asset, debit, credit = await seed(session, "validation")
        await session.commit()
        baseline = await counts(session)
        await session.rollback()
        service = LedgerPostingService(session)
        async with session.begin():
            for changed in (
                {"idempotency_key": " key"},
                {"source_namespace": ""},
                {"source_external_id": 3},
                {"source_digest": "f" * 63},
                {
                    "lines": (
                        LedgerLine(debit, asset, "debit", 100),
                        LedgerLine(credit, asset, LedgerDirection.CREDIT, 100),
                    )
                },
            ):
                with pytest.raises(LedgerValidationError):
                    await service.post_in_transaction(
                        **{
                            **request(
                                merchant,
                                asset,
                                debit,
                                credit,
                                key=f"validation-{uuid.uuid4()}",
                                source=str(uuid.uuid4()),
                            ),
                            **changed,
                        }
                    )
        assert await counts(session) == baseline
