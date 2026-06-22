import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.db.models import (
    AddressLeaseEvent,
    Base,
    DepositAddress,
    DepositAddressLeaseStatus,
    Invoice,
    InvoiceStatus,
    Merchant,
    PaymentSession,
    PaymentSessionStatus,
)
from src.services.address_lease_service import AddressLeaseService


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


def make_address(
    *,
    suffix: str,
    status: DepositAddressLeaseStatus = DepositAddressLeaseStatus.AVAILABLE,
    is_used: bool = False,
    cooldown_until: datetime | None = None,
) -> DepositAddress:
    return DepositAddress(
        id=uuid.uuid4(),
        address=f"0x{'0' * (40 - len(suffix))}{suffix}",
        encrypted_privkey=b"encrypted",
        chain_group="evm",
        derivation_path=f"m/44'/60'/0'/0/{int(suffix, 16)}",
        derivation_index=int(suffix, 16),
        is_used=is_used,
        lease_status=status,
        cooldown_until=cooldown_until,
    )


@pytest.mark.asyncio
async def test_acquire_available_address_marks_leased_and_records_event(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    leased_until = now + timedelta(minutes=30)
    address = make_address(suffix="1")
    db_session.add(address)
    await db_session.commit()

    service = AddressLeaseService(db_session)
    leased = await service.acquire_available_address("evm", leased_until, now=now)
    await db_session.commit()

    assert leased is not None
    assert leased.id == address.id
    assert leased.lease_status == DepositAddressLeaseStatus.LEASED
    assert leased.is_used is True
    assert leased.leased_until == leased_until
    assert leased.cooldown_until is None

    events = (
        await db_session.execute(
            select(AddressLeaseEvent).where(AddressLeaseEvent.deposit_address_id == address.id)
        )
    ).scalars().all()
    assert [event.event_type for event in events] == ["lease_acquired"]
    assert events[0].new_status == DepositAddressLeaseStatus.LEASED.value


@pytest.mark.asyncio
async def test_cooldown_address_reused_only_after_promotion(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cooldown_until = now + timedelta(minutes=10)
    address = make_address(
        suffix="2",
        status=DepositAddressLeaseStatus.COOLDOWN,
        is_used=True,
        cooldown_until=cooldown_until,
    )
    db_session.add(address)
    await db_session.commit()

    service = AddressLeaseService(db_session)
    assert (
        await service.acquire_available_address("evm", now + timedelta(minutes=30), now=now)
        is None
    )

    leased = await service.acquire_available_address(
        "evm",
        now + timedelta(hours=1),
        now=cooldown_until + timedelta(seconds=1),
    )
    await db_session.commit()

    assert leased is not None
    assert leased.id == address.id
    assert leased.lease_status == DepositAddressLeaseStatus.LEASED
    assert leased.is_used is True

    event_types = (
        await db_session.execute(
            select(AddressLeaseEvent.event_type).where(
                AddressLeaseEvent.deposit_address_id == address.id
            )
        )
    ).scalars().all()
    assert event_types == ["cooldown_completed", "lease_acquired"]


@pytest.mark.asyncio
async def test_release_to_cooldown_updates_session_address_and_event(db_session):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    cooldown_until = now + timedelta(hours=2)
    merchant = Merchant(id=uuid.uuid4(), name="merchant", email="merchant@example.com")
    invoice = Invoice(
        id=uuid.uuid4(),
        public_id="test-invoice",
        merchant_id=merchant.id,
        amount=Decimal("10.00"),
        asset="USDT",
        allowed_chains=["bsc"],
        status=InvoiceStatus.AWAITING_PAYMENT,
        ttl_minutes=30,
        expires_at=now + timedelta(minutes=30),
    )
    address = make_address(
        suffix="3",
        status=DepositAddressLeaseStatus.LEASED,
        is_used=True,
    )
    payment_session = PaymentSession(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        chain="bsc",
        token="USDT",
        deposit_address_id=address.id,
        status=PaymentSessionStatus.PENDING,
        expires_at=invoice.expires_at,
    )
    db_session.add_all([merchant, invoice, address, payment_session])
    await db_session.commit()

    service = AddressLeaseService(db_session)
    await service.release_to_cooldown(
        payment_session,
        cooldown_until,
        status=PaymentSessionStatus.EXPIRED,
        reason="invoice_expired",
        now=now,
    )
    await db_session.commit()

    assert payment_session.status == PaymentSessionStatus.EXPIRED
    assert payment_session.released_at == now
    assert address.lease_status == DepositAddressLeaseStatus.COOLDOWN
    assert address.is_used is True
    assert address.leased_until is None
    assert address.cooldown_until == cooldown_until

    events = (
        await db_session.execute(
            select(AddressLeaseEvent).where(
                AddressLeaseEvent.payment_session_id == payment_session.id
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "lease_released_to_cooldown"
    assert events[0].new_status == DepositAddressLeaseStatus.COOLDOWN.value
