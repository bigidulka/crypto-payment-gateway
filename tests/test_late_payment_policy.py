import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

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
    SweepSource,
    SweepState,
    UnifiedSweepJob,
)
from src.services.payment_service import PaymentService
from src.workers.evm_log_poller import get_active_deposit_addresses


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_late_payment_creates_sweep_without_confirming_invoice(
    db_session,
    monkeypatch,
):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    merchant = Merchant(
        id=uuid.uuid4(),
        name="merchant",
        email="late-policy@example.com",
    )
    invoice = Invoice(
        id=uuid.uuid4(),
        public_id="late-payment",
        merchant_id=merchant.id,
        amount=Decimal("10.00"),
        asset="USDT",
        allowed_chains=["bsc"],
        status=InvoiceStatus.EXPIRED,
        ttl_minutes=30,
        expires_at=now - timedelta(minutes=1),
    )
    address = DepositAddress(
        id=uuid.uuid4(),
        address="0x" + "a" * 40,
        encrypted_privkey=b"encrypted-private-key",
        chain_group="evm",
        derivation_path="m/44'/60'/0'/0/1",
        derivation_index=1,
        is_used=True,
        lease_status=DepositAddressLeaseStatus.COOLDOWN,
        cooldown_until=now + timedelta(minutes=30),
    )
    payment_session = PaymentSession(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        chain="bsc",
        token="USDT",
        deposit_address_id=address.id,
        status=PaymentSessionStatus.EXPIRED,
        expires_at=invoice.expires_at,
        released_at=now,
    )
    db_session.add_all([merchant, invoice, address, payment_session])
    await db_session.commit()
    await db_session.refresh(payment_session, ["deposit_address", "invoice"])

    monkeypatch.setattr(
        "src.services.payment_service.get_settings",
        lambda: SimpleNamespace(
            get_treasury_address=lambda chain: "0x" + "f" * 40,
        ),
    )

    service = PaymentService(db_session)
    await service.process_late_payment(payment_session, invoice)

    jobs = (await db_session.execute(select(UnifiedSweepJob))).scalars().all()
    events = (await db_session.execute(select(AddressLeaseEvent))).scalars().all()

    assert invoice.status == InvoiceStatus.EXPIRED
    assert payment_session.status == PaymentSessionStatus.LATE
    assert payment_session.paid_at is not None
    assert len(jobs) == 1
    assert jobs[0].source == SweepSource.INVOICE
    assert jobs[0].source_id == payment_session.id
    assert jobs[0].state == SweepState.PENDING_GAS
    assert jobs[0].priority == 0
    assert jobs[0].from_address == address.address
    assert jobs[0].to_address == "0x" + "f" * 40
    assert [event.event_type for event in events] == ["late_deposit_detected"]


@pytest.mark.asyncio
async def test_late_payment_processing_is_idempotent(db_session, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    merchant = Merchant(
        id=uuid.uuid4(),
        name="merchant",
        email="late-idempotent@example.com",
    )
    invoice = Invoice(
        id=uuid.uuid4(),
        public_id="late-idempotent",
        merchant_id=merchant.id,
        amount=Decimal("5.00"),
        asset="USDT",
        allowed_chains=["bsc"],
        status=InvoiceStatus.EXPIRED,
        ttl_minutes=30,
        expires_at=now - timedelta(minutes=1),
    )
    address = DepositAddress(
        id=uuid.uuid4(),
        address="0x" + "b" * 40,
        encrypted_privkey=b"encrypted-private-key",
        chain_group="evm",
        derivation_path="m/44'/60'/0'/0/2",
        derivation_index=2,
        is_used=True,
        lease_status=DepositAddressLeaseStatus.COOLDOWN,
        cooldown_until=now + timedelta(minutes=30),
    )
    payment_session = PaymentSession(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        chain="bsc",
        token="USDT",
        deposit_address_id=address.id,
        status=PaymentSessionStatus.LATE,
        expires_at=invoice.expires_at,
        released_at=now,
        paid_at=now,
    )
    db_session.add_all([merchant, invoice, address, payment_session])
    await db_session.commit()
    await db_session.refresh(payment_session, ["deposit_address", "invoice"])

    monkeypatch.setattr(
        "src.services.payment_service.get_settings",
        lambda: SimpleNamespace(
            get_treasury_address=lambda chain: "0x" + "f" * 40,
        ),
    )

    service = PaymentService(db_session)
    await service.process_late_payment(payment_session, invoice)
    await service.process_late_payment(payment_session, invoice)

    jobs = (await db_session.execute(select(UnifiedSweepJob))).scalars().all()
    events = (await db_session.execute(select(AddressLeaseEvent))).scalars().all()

    assert invoice.status == InvoiceStatus.EXPIRED
    assert payment_session.status == PaymentSessionStatus.LATE
    assert len(jobs) == 1
    assert events == []


@pytest.mark.asyncio
async def test_expired_session_scanned_only_during_address_cooldown(db_session):
    now = datetime.now(UTC)
    merchant = Merchant(
        id=uuid.uuid4(),
        name="merchant",
        email="late-scan-window@example.com",
    )
    invoice = Invoice(
        id=uuid.uuid4(),
        public_id="late-scan-window",
        merchant_id=merchant.id,
        amount=Decimal("5.00"),
        asset="USDT",
        allowed_chains=["bsc"],
        status=InvoiceStatus.EXPIRED,
        ttl_minutes=30,
        expires_at=now - timedelta(minutes=1),
    )
    address = DepositAddress(
        id=uuid.uuid4(),
        address="0x" + "c" * 40,
        encrypted_privkey=b"encrypted-private-key",
        chain_group="evm",
        derivation_path="m/44'/60'/0'/0/3",
        derivation_index=3,
        is_used=True,
        lease_status=DepositAddressLeaseStatus.COOLDOWN,
        cooldown_until=now + timedelta(minutes=30),
    )
    payment_session = PaymentSession(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        chain="bsc",
        token="USDT",
        deposit_address_id=address.id,
        status=PaymentSessionStatus.EXPIRED,
        expires_at=invoice.expires_at,
        released_at=now,
    )
    db_session.add_all([merchant, invoice, address, payment_session])
    await db_session.commit()

    in_cooldown = await get_active_deposit_addresses(db_session, "bsc")
    address.cooldown_until = now - timedelta(seconds=1)
    address.lease_status = DepositAddressLeaseStatus.AVAILABLE
    address.is_used = False
    await db_session.commit()
    after_cooldown = await get_active_deposit_addresses(db_session, "bsc")

    assert in_cooldown == {address.address.lower(): payment_session}
    assert after_cooldown == {}


@pytest.mark.asyncio
async def test_late_scan_uses_latest_terminal_session_for_reused_address(db_session):
    now = datetime.now(UTC)
    merchant = Merchant(
        id=uuid.uuid4(),
        name="merchant",
        email="late-latest-session@example.com",
    )
    older_invoice = _expired_invoice(merchant.id, "late-old", now)
    newer_invoice = _expired_invoice(merchant.id, "late-new", now)
    address = DepositAddress(
        id=uuid.uuid4(),
        address="0x" + "d" * 40,
        encrypted_privkey=b"encrypted-private-key",
        chain_group="evm",
        derivation_path="m/44'/60'/0'/0/4",
        derivation_index=4,
        is_used=True,
        lease_status=DepositAddressLeaseStatus.COOLDOWN,
        cooldown_until=now + timedelta(minutes=30),
    )
    older_session = _expired_session(
        older_invoice,
        address,
        released_at=now - timedelta(hours=1),
    )
    newer_session = _expired_session(
        newer_invoice,
        address,
        released_at=now,
    )
    db_session.add_all(
        [merchant, older_invoice, newer_invoice, address, older_session, newer_session]
    )
    await db_session.commit()

    address_map = await get_active_deposit_addresses(db_session, "bsc")

    assert address_map == {address.address.lower(): newer_session}


def _expired_invoice(merchant_id: uuid.UUID, public_id: str, now: datetime) -> Invoice:
    return Invoice(
        id=uuid.uuid4(),
        public_id=public_id,
        merchant_id=merchant_id,
        amount=Decimal("5.00"),
        asset="USDT",
        allowed_chains=["bsc"],
        status=InvoiceStatus.EXPIRED,
        ttl_minutes=30,
        expires_at=now - timedelta(minutes=1),
    )


def _expired_session(
    invoice: Invoice,
    address: DepositAddress,
    *,
    released_at: datetime,
) -> PaymentSession:
    return PaymentSession(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        chain="bsc",
        token="USDT",
        deposit_address_id=address.id,
        status=PaymentSessionStatus.EXPIRED,
        expires_at=invoice.expires_at,
        released_at=released_at,
    )
