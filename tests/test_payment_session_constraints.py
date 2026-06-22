import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import (
    Base,
    DepositAddress,
    DepositAddressLeaseStatus,
    Invoice,
    InvoiceStatus,
    Merchant,
    PaymentSession,
    PaymentSessionStatus,
)


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


def test_payment_session_model_has_partial_active_address_unique_index():
    index = next(
        idx
        for idx in PaymentSession.__table__.indexes
        if idx.name == "uq_payment_session_address_active"
    )

    assert index.unique is True
    assert [column.name for column in index.columns] == ["deposit_address_id"]
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "status IN ('pending', 'seen_onchain')"
    )
    assert str(index.dialect_options["sqlite"]["where"]) == (
        "status IN ('pending', 'seen_onchain')"
    )


@pytest.mark.asyncio
async def test_db_rejects_two_active_sessions_for_one_address(db_session):
    merchant, address, invoices = _make_base_objects(2)
    db_session.add_all([merchant, address, *invoices])
    await db_session.commit()

    db_session.add_all(
        [
            _make_session(invoices[0], address, PaymentSessionStatus.PENDING),
            _make_session(invoices[1], address, PaymentSessionStatus.SEEN_ONCHAIN),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_db_allows_reusing_address_after_terminal_sessions(db_session):
    merchant, address, invoices = _make_base_objects(2)
    db_session.add_all([merchant, address, *invoices])
    await db_session.commit()

    db_session.add_all(
        [
            _make_session(invoices[0], address, PaymentSessionStatus.EXPIRED),
            _make_session(invoices[1], address, PaymentSessionStatus.PAID),
        ]
    )
    await db_session.commit()


def _make_base_objects(count: int):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    merchant = Merchant(
        id=uuid.uuid4(),
        name="merchant",
        email=f"constraints-{uuid.uuid4().hex}@example.com",
    )
    address = DepositAddress(
        id=uuid.uuid4(),
        address="0x" + "a" * 40,
        encrypted_privkey=b"encrypted",
        chain_group="evm",
        derivation_path="m/44'/60'/0'/0/1",
        derivation_index=1,
        is_used=False,
        lease_status=DepositAddressLeaseStatus.AVAILABLE,
    )
    invoices = [
        Invoice(
            id=uuid.uuid4(),
            public_id=f"constraint-{uuid.uuid4().hex[:12]}",
            merchant_id=merchant.id,
            amount=Decimal("10"),
            asset="USDT",
            allowed_chains=["bsc"],
            status=InvoiceStatus.AWAITING_PAYMENT,
            ttl_minutes=30,
            expires_at=now + timedelta(minutes=30),
        )
        for _ in range(count)
    ]
    return merchant, address, invoices


def _make_session(
    invoice: Invoice,
    address: DepositAddress,
    status: PaymentSessionStatus,
) -> PaymentSession:
    return PaymentSession(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        chain="bsc",
        token="USDT",
        deposit_address_id=address.id,
        status=status,
        expires_at=invoice.expires_at,
    )
