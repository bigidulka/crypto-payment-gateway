"""Owner-only isolated PostgreSQL checks for the disabled provider ingestion slice.

These tests deliberately do not claim runtime-role coverage. A separately
guarded runtime fixture is provided for the next checkpoint, when reviewed
non-owner grants and an explicit runtime database are available.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.db.models import (
    Invoice,
    InvoiceEvent,
    InvoiceStatus,
    LedgerAccount,
    LedgerAsset,
    LedgerEntry,
    LedgerTransaction,
    Merchant,
    OutboxWebhook,
    ProviderPaymentMapping,
    ProviderPaymentObservation,
    ProviderPaymentReceipt,
    ProviderPaymentRecipientIntent,
    ProviderVerificationDecision,
    Rail,
    Webhook,
)
from src.ledger.amounts import decimal_to_atomic
from src.payments.rails.base import RailPayment
from src.services.provider_payment_ingestion import (
    ProviderIngestionDisabled,
    ProviderPaymentIngestionService,
    ProviderReconciliationConflict,
    ProviderVerificationBinding,
)

OWNER_DATABASE_ENV = "PROVIDER_INGESTION_OWNER_DATABASE_URL"
RUNTIME_DATABASE_ENV = "PROVIDER_INGESTION_RUNTIME_DATABASE_URL"
EXPECTED_REVISION = "0011_provider_payment_ingestion"


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def settings(*, enabled: bool, merchant_id: uuid.UUID | None = None) -> Settings:
    return Settings(
        secret_key="x" * 32,
        encryption_key="x" * 32,
        ledger_rail_orchestration_enabled=enabled,
        ledger_rail_orchestration_merchants=str(merchant_id) if merchant_id else "",
    )


async def _checked_factory(environment_name: str):
    database_url = os.getenv(environment_name)
    if not database_url:
        pytest.fail(
            f"{environment_name} must identify an explicit disposable test_* PostgreSQL database"
        )
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host not in {"127.0.0.1", "::1"}:
        pytest.fail(f"{environment_name} must use a loopback postgresql+asyncpg URL")
    if not parsed.database or not parsed.database.startswith("test_"):
        pytest.fail(f"{environment_name} database must begin with test_")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if current_database != parsed.database or not current_database.startswith("test_"):
                pytest.fail(
                    f"{environment_name} current database marker is not the requested test database"
                )
            if revision != EXPECTED_REVISION:
                pytest.fail(f"{environment_name} must be migrated to {EXPECTED_REVISION}")
        return async_sessionmaker(engine, expire_on_commit=False)
    except BaseException:
        await engine.dispose()
        raise


@pytest_asyncio.fixture
async def owner_session_factory():
    """Owner-migration database fixture; no test here claims runtime-role evidence."""
    factory = await _checked_factory(OWNER_DATABASE_ENV)
    try:
        yield factory
    finally:
        await factory.kw["bind"].dispose()


@pytest_asyncio.fixture
async def runtime_app_session_factory():
    owner_url = os.getenv(OWNER_DATABASE_ENV)
    runtime_url = os.getenv(RUNTIME_DATABASE_ENV)
    if not runtime_url or runtime_url == owner_url:
        pytest.fail(f"{RUNTIME_DATABASE_ENV} must use distinct explicit non-owner credentials")
    factory = await _checked_factory(RUNTIME_DATABASE_ENV)
    try:
        yield factory
    finally:
        await factory.kw["bind"].dispose()


@asynccontextmanager
async def rollback_session(factory):
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


async def seed(
    session: AsyncSession,
    label: str,
    *,
    merchant: Merchant | None = None,
    network: str = "MAIN_NET",
    provider_account_identity: str | None = None,
    provider_invoice_id: str | None = None,
):
    merchant = merchant or Merchant(
        name=f"provider {label}", email=f"provider-{label}-{uuid.uuid4()}@test.invalid"
    )
    invoice = Invoice(
        public_id=f"provider-{uuid.uuid4().hex[:20]}",
        merchant=merchant,
        amount=Decimal("12.34"),
        asset="USDT",
        allowed_chains=["cryptobot"],
        status=InvoiceStatus.AWAITING_PAYMENT,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    rail = Rail(merchant=merchant, rail_type="cryptobot", network=network, assets=["USDT"])
    if merchant.id is None:
        session.add(merchant)
    session.add_all([invoice, rail])
    await session.flush()
    asset = await session.scalar(
        select(LedgerAsset).where(
            LedgerAsset.network_kind == "custodial",
            LedgerAsset.network_identifier == f"cryptobot:{network}",
            LedgerAsset.canonical_identifier == "USDT",
            LedgerAsset.is_native.is_(False),
        )
    )
    if asset is None:
        asset = LedgerAsset(
            network_kind="custodial",
            network_identifier=f"cryptobot:{network}",
            canonical_identifier="USDT",
            is_native=False,
            atomic_decimals=2,
            symbol="USDT",
        )
        session.add(asset)
        await session.flush()
    receivable = LedgerAccount(
        merchant_id=merchant.id,
        account_type="merchant_custodial_receivable",
        custody_type="merchant_custodial",
    )
    payable = LedgerAccount(
        merchant_id=merchant.id,
        account_type="merchant_payable",
        custody_type="merchant_liability",
    )
    webhook = Webhook(
        merchant_id=merchant.id,
        url="https://merchant.example.test/provider-ingestion",
        secret="x" * 32,
        events=["invoice.confirmed"],
    )
    session.add_all([asset, receivable, payable, webhook])
    await session.flush()
    mapping = ProviderPaymentMapping(
        merchant_id=merchant.id,
        invoice_id=invoice.id,
        rail_id=rail.id,
        provider_account_identity=provider_account_identity
        or sha(f"cryptobot-account:{uuid.uuid4()}"),
        provider_network=network,
        provider_invoice_id=provider_invoice_id or f"provider-{uuid.uuid4().hex}",
        provider_asset_code="USDT",
        ledger_asset_id=asset.id,
        expected_amount_atomic=1234,
        receivable_account_id=receivable.id,
        payable_account_id=payable.id,
    )
    session.add(mapping)
    await session.flush()
    return merchant, invoice, asset, mapping


class TrustedFakeVerifier:
    def __init__(
        self,
        binding: ProviderVerificationBinding,
        *,
        amount: Decimal = Decimal("12.34"),
        asset: str = "USDT",
        provider_invoice_id: str | None = None,
        paid_at: str = "2026-09-05T00:00:00Z",
        raw: dict | None = None,
    ):
        self.binding = binding
        self.amount = amount
        self.asset = asset
        self.provider_invoice_id = provider_invoice_id
        self.paid_at = paid_at
        self.raw = raw or {"status": "paid"}
        self.calls: list[str] = []

    async def verify_payment(self, provider_invoice_id: str):
        self.calls.append(provider_invoice_id)
        return RailPayment(
            provider_invoice_id=self.provider_invoice_id or provider_invoice_id,
            amount=self.amount,
            asset=self.asset,
            paid_at=self.paid_at,
            raw=self.raw,
        )


def binding(merchant: Merchant, mapping: ProviderPaymentMapping) -> ProviderVerificationBinding:
    return ProviderVerificationBinding(
        merchant_id=merchant.id,
        mapping_id=mapping.id,
        rail_id=mapping.rail_id,
        provider_account_identity=mapping.provider_account_identity,
        provider_network=mapping.provider_network,
        ledger_asset_id=mapping.ledger_asset_id,
    )


async def counts(
    session: AsyncSession, merchant_id: uuid.UUID
) -> tuple[int, int, int, int, int, int, int, int]:
    observations = await session.scalar(
        select(func.count())
        .select_from(ProviderPaymentObservation)
        .where(ProviderPaymentObservation.merchant_id == merchant_id)
    )
    decisions = await session.scalar(
        select(func.count())
        .select_from(ProviderVerificationDecision)
        .where(ProviderVerificationDecision.merchant_id == merchant_id)
    )
    receipts = await session.scalar(
        select(func.count())
        .select_from(ProviderPaymentReceipt)
        .where(ProviderPaymentReceipt.merchant_id == merchant_id)
    )
    intents = await session.scalar(
        select(func.count())
        .select_from(ProviderPaymentRecipientIntent)
        .join(ProviderPaymentReceipt)
        .where(ProviderPaymentReceipt.merchant_id == merchant_id)
    )
    transactions = await session.scalar(
        select(func.count())
        .select_from(LedgerTransaction)
        .where(LedgerTransaction.merchant_id == merchant_id)
    )
    entries = await session.scalar(
        select(func.count()).select_from(LedgerEntry).where(LedgerEntry.merchant_id == merchant_id)
    )
    events = await session.scalar(
        select(func.count())
        .select_from(InvoiceEvent)
        .join(Invoice)
        .where(Invoice.merchant_id == merchant_id)
    )
    outbox = await session.scalar(
        select(func.count())
        .select_from(OutboxWebhook)
        .join(Invoice)
        .where(Invoice.merchant_id == merchant_id)
    )
    return observations, decisions, receipts, intents, transactions, entries, events, outbox


@pytest.mark.parametrize(
    ("amount", "decimals", "expected"),
    [
        (Decimal("12.34"), 2, 1234),
        (Decimal("1" + "0" * 77), 0, 10**77),
        (Decimal("9" * 77), 0, 10**77 - 1),
    ],
)
def test_exact_atomic_conversion_ignores_low_decimal_context(amount, decimals, expected):
    with localcontext() as context:
        context.prec = 5
        assert decimal_to_atomic(amount, decimals) == expected


def test_exact_atomic_conversion_normalizes_long_trailing_zero_representation():
    with localcontext() as context:
        context.prec = 5
        assert decimal_to_atomic(Decimal("1." + "0" * 5000), 0) == 1
    with pytest.raises(ValueError, match="range"):
        decimal_to_atomic(Decimal("9" * 5000), 0)


@pytest.mark.parametrize(
    "amount",
    [
        True,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("0"),
        Decimal("0.001"),
        Decimal(10) ** 78,
        Decimal("1E999999"),
    ],
)
def test_exact_atomic_conversion_rejects_invalid_precision_and_bounds(amount):
    with pytest.raises(ValueError):
        decimal_to_atomic(amount, 2)


@pytest.mark.asyncio
async def test_disabled_or_unallowlisted_actor_does_no_database_or_provider_io(
    owner_session_factory,
):
    class NeverCalled:
        binding = None

        async def verify_payment(self, provider_invoice_id: str):
            raise AssertionError(f"provider must not be called: {provider_invoice_id}")

    async with rollback_session(owner_session_factory) as session:
        merchant, _, _, mapping = await seed(session, "disabled")
        await session.commit()
        service = ProviderPaymentIngestionService(session, settings=settings(enabled=False))
        with pytest.raises(ProviderIngestionDisabled):
            await service.verify_provider_invoice(merchant.id, mapping.id, NeverCalled())
        service = ProviderPaymentIngestionService(session, settings=settings(enabled=True))
        with pytest.raises(ProviderIngestionDisabled):
            await service.verify_provider_invoice(merchant.id, mapping.id, NeverCalled())


@pytest.mark.asyncio
async def test_bound_verification_and_atomic_consume_are_exact_and_owner_only(
    owner_session_factory,
):
    async with rollback_session(owner_session_factory) as session:
        merchant, invoice, _, mapping = await seed(session, "exact")
        await session.commit()
        merchant_id, mapping_id, provider_invoice_id = (
            merchant.id,
            mapping.id,
            mapping.provider_invoice_id,
        )
        trusted_binding = binding(merchant, mapping)
        verifier = TrustedFakeVerifier(trusted_binding, raw={"status": "paid", "api_token": "x"})
        service = ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        observation = await service.verify_provider_invoice(merchant_id, mapping_id, verifier)
        assert observation is not None
        assert verifier.calls == [provider_invoice_id]
        assert observation.merchant_id == merchant_id
        assert observation.mapping_id == mapping_id
        assert observation.observed_amount_atomic == 1234
        assert observation.evidence_digest != sha("x")
        await session.rollback()
        async with session.begin():
            fact = await service.consume_verified_observation(merchant_id, observation)
        await session.refresh(invoice)
        assert fact.mapping_id == mapping_id
        assert invoice.status is InvoiceStatus.CONFIRMED
        assert await counts(session, merchant_id) == (1, 1, 1, 1, 1, 2, 1, 1)
        receipt = await session.scalar(
            select(ProviderPaymentReceipt).where(ProviderPaymentReceipt.observation_id == fact.id)
        )
        assert receipt is not None and receipt.intended_recipient_count == 1
        intent = await session.scalar(
            select(ProviderPaymentRecipientIntent).where(
                ProviderPaymentRecipientIntent.receipt_id == receipt.id
            )
        )
        event = await session.scalar(
            select(InvoiceEvent).where(InvoiceEvent.id == receipt.invoice_event_id)
        )
        assert intent is not None
        assert event.event_id == receipt.event_id
        assert event.payload["observed"]["ledger_asset_id"] == str(fact.ledger_asset_id)
        assert event.payload["observed"]["provider_network"] == "MAIN_NET"
        assert event.payload["observed"]["amount_atomic"] == "1234"


@pytest.mark.asyncio
async def test_same_provider_account_and_invoice_on_distinct_networks_are_independent(
    owner_session_factory,
):
    shared_account = sha(f"shared-account:{uuid.uuid4()}")
    shared_invoice = f"shared-invoice-{uuid.uuid4().hex}"
    async with rollback_session(owner_session_factory) as session:
        merchant, _, _, main_mapping = await seed(
            session,
            "network-main",
            provider_account_identity=shared_account,
            provider_invoice_id=shared_invoice,
        )
        _, _, _, test_mapping = await seed(
            session,
            "network-test",
            merchant=merchant,
            network="TEST_NET",
            provider_account_identity=shared_account,
            provider_invoice_id=shared_invoice,
        )
        merchant_id = merchant.id
        main_mapping_id, test_mapping_id = main_mapping.id, test_mapping.id
        main_binding, test_binding = (
            binding(merchant, main_mapping),
            binding(merchant, test_mapping),
        )
        await session.commit()
        service = ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        main_observation = await service.verify_provider_invoice(
            merchant_id, main_mapping_id, TrustedFakeVerifier(main_binding)
        )
        test_observation = await service.verify_provider_invoice(
            merchant_id, test_mapping_id, TrustedFakeVerifier(test_binding)
        )
        assert main_observation is not None and test_observation is not None
        await session.rollback()
        async with session.begin():
            main_fact = await service.consume_verified_observation(merchant_id, main_observation)
        async with session.begin():
            test_fact = await service.consume_verified_observation(merchant_id, test_observation)
        assert main_fact.id != test_fact.id
        assert {main_fact.provider_network, test_fact.provider_network} == {
            "MAIN_NET",
            "TEST_NET",
        }
        with pytest.raises(ProviderReconciliationConflict):
            await service.consume_verified_observation(
                merchant_id,
                replace(test_observation, provider_network="MAIN_NET"),
            )


@pytest.mark.asyncio
async def test_receipt_guard_locks_committed_parent_event_before_payload_update(
    owner_session_factory,
):
    async with owner_session_factory() as setup:
        merchant, _, _, mapping = await seed(setup, "event-lock")
        await setup.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        invoice_id, asset_id = mapping.invoice_id, mapping.ledger_asset_id
        receivable_account_id, payable_account_id = (
            mapping.receivable_account_id,
            mapping.payable_account_id,
        )
        provider_asset_code = mapping.provider_asset_code
        verified = await ProviderPaymentIngestionService(
            setup, settings=settings(enabled=True, merchant_id=merchant_id)
        ).verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(binding(merchant, mapping))
        )
        assert verified is not None
        await setup.rollback()
        async with setup.begin():
            fact = ProviderPaymentObservation(
                merchant_id=merchant_id,
                mapping_id=mapping_id,
                rail_id=verified.rail_id,
                provider_account_identity=verified.provider_account_identity,
                provider_network=verified.provider_network,
                provider_invoice_id=verified.provider_invoice_id,
                ledger_asset_id=verified.ledger_asset_id,
                observed_amount_atomic=verified.observed_amount_atomic,
                observed_at=datetime.now(UTC),
                source_kind=verified.source_kind,
                economic_digest=verified.economic_digest,
                evidence_digest=verified.evidence_digest,
            )
            setup.add(fact)
            await setup.flush()
            decision = ProviderVerificationDecision(
                merchant_id=merchant_id,
                observation_id=fact.id,
                decision="verified",
                decision_digest=sha(f"decision:{fact.id}"),
            )
            setup.add(decision)
            transaction = LedgerTransaction(
                merchant_id=merchant_id,
                status="open",
                source_namespace="provider_payment_observation",
                source_external_id=fact.economic_digest,
                source_digest=fact.economic_digest,
                idempotency_key=f"event-lock:{uuid.uuid4()}",
                posting_digest=sha(f"posting:{fact.id}"),
            )
            setup.add(transaction)
            invoice = await setup.scalar(select(Invoice).where(Invoice.id == invoice_id))
            asset = await setup.scalar(select(LedgerAsset).where(LedgerAsset.id == asset_id))
            assert invoice is not None and asset is not None
            invoice.status = InvoiceStatus.CONFIRMED
            payload = {
                "event": "invoice.confirmed",
                "event_id": fact.economic_digest,
                "invoice": {
                    "id": str(invoice.id),
                    "public_id": invoice.public_id,
                    "status": "CONFIRMED",
                },
                "observed": {
                    "ledger_asset_id": str(asset.id),
                    "network_kind": asset.network_kind,
                    "network_identifier": asset.network_identifier,
                    "atomic_decimals": asset.atomic_decimals,
                    "provider_account_identity": fact.provider_account_identity,
                    "provider_network": fact.provider_network,
                    "asset_code": provider_asset_code,
                    "amount_atomic": str(fact.observed_amount_atomic),
                },
            }
            event = InvoiceEvent(
                invoice_id=invoice.id,
                event_id=fact.economic_digest,
                event_type="invoice.confirmed",
                payload=payload,
            )
            setup.add(event)
            await setup.flush()
            setup.add_all(
                [
                    LedgerEntry(
                        merchant_id=merchant_id,
                        ledger_transaction_id=transaction.id,
                        ledger_account_id=receivable_account_id,
                        ledger_asset_id=asset.id,
                        direction="debit",
                        amount_atomic=fact.observed_amount_atomic,
                    ),
                    LedgerEntry(
                        merchant_id=merchant_id,
                        ledger_transaction_id=transaction.id,
                        ledger_account_id=payable_account_id,
                        ledger_asset_id=asset.id,
                        direction="credit",
                        amount_atomic=fact.observed_amount_atomic,
                    ),
                ]
            )
            await setup.flush()
            transaction.status = "posted"
            transaction.posted_at = datetime.now(UTC)
        receipt_values = {
            "id": str(uuid.uuid4()),
            "merchant": str(merchant_id),
            "observation": str(fact.id),
            "decision": str(decision.id),
            "transaction": str(transaction.id),
            "invoice": str(invoice.id),
            "event": str(event.id),
            "event_id": fact.economic_digest,
        }

    insert_receipt = text(
        "INSERT INTO provider_payment_receipts "
        "(id,merchant_id,observation_id,decision_id,ledger_transaction_id,invoice_id,invoice_event_id,event_id,intended_recipient_count) "
        "VALUES (:id,:merchant,:observation,:decision,:transaction,:invoice,:event,:event_id,0)"
    )
    async with (
        owner_session_factory() as receipt_tx,
        owner_session_factory() as updater,
        owner_session_factory() as observer,
    ):
        await receipt_tx.begin()
        await receipt_tx.execute(insert_receipt, receipt_values)
        receipt_pid = await receipt_tx.scalar(text("SELECT pg_backend_pid()"))
        updater_pid = await updater.scalar(text("SELECT pg_backend_pid()"))
        await updater.execute(text("SET lock_timeout = '500ms'"))
        update_task = asyncio.create_task(
            updater.execute(
                text("UPDATE invoice_events SET payload='{}'::jsonb WHERE id=:event"),
                {"event": receipt_values["event"]},
            )
        )
        blocked = False
        for _ in range(20):
            blocked = bool(
                await observer.scalar(
                    text("SELECT :receipt_pid = ANY(pg_blocking_pids(:updater_pid))"),
                    {"receipt_pid": receipt_pid, "updater_pid": updater_pid},
                )
            )
            if blocked:
                break
            await asyncio.sleep(0.025)
        assert blocked
        with pytest.raises(DBAPIError) as blocked_update:
            await update_task
        assert getattr(blocked_update.value.orig, "sqlstate", None) == "55P03"
        await updater.rollback()
        await receipt_tx.commit()
        with pytest.raises(DBAPIError) as post_commit_update:
            await updater.execute(
                text("UPDATE invoice_events SET payload='{}'::jsonb WHERE id=:event"),
                {"event": receipt_values["event"]},
            )
        assert getattr(post_commit_update.value.orig, "sqlstate", None) == "P0001"
        await updater.rollback()


@pytest.mark.asyncio
async def test_runtime_role_posts_once_and_cannot_ddl_or_truncate(
    owner_session_factory,
    runtime_app_session_factory,
):
    async with owner_session_factory() as owner:
        merchant, _, _, mapping = await seed(owner, "runtime-role")
        await owner.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        observation = await ProviderPaymentIngestionService(
            owner, settings=settings(enabled=True, merchant_id=merchant_id)
        ).verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(binding(merchant, mapping))
        )
        assert observation is not None

    async with runtime_app_session_factory() as runtime:
        assert (await runtime.scalar(text("SELECT current_user"))) == "provider_ingestion_runtime"
        await runtime.rollback()
        service = ProviderPaymentIngestionService(
            runtime, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        async with runtime.begin():
            first = await service.consume_verified_observation(merchant_id, observation)
        async with runtime.begin():
            second = await service.consume_verified_observation(
                merchant_id, replace(observation, source_kind="provider_webhook")
            )
        assert first.id == second.id
        for statement in (
            "CREATE TABLE forbidden_runtime_ddl(id integer)",
            "TRUNCATE provider_payment_observations",
        ):
            with pytest.raises(ProgrammingError) as exc:
                await runtime.execute(text(statement))
            assert getattr(exc.value.orig, "sqlstate", None) == "42501"
            await runtime.rollback()
    async with owner_session_factory() as verify:
        assert await counts(verify, merchant_id) == (1, 1, 1, 1, 1, 2, 1, 1)


@pytest.mark.asyncio
async def test_runtime_concurrent_poll_webhook_posts_one_complete_projection(
    owner_session_factory,
    runtime_app_session_factory,
):
    async with owner_session_factory() as owner:
        merchant, _, _, mapping = await seed(owner, "runtime-concurrent")
        await owner.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        observation = await ProviderPaymentIngestionService(
            owner, settings=settings(enabled=True, merchant_id=merchant_id)
        ).verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(binding(merchant, mapping))
        )
        assert observation is not None

    async def consume(source_kind: str):
        async with runtime_app_session_factory() as runtime:
            service = ProviderPaymentIngestionService(
                runtime, settings=settings(enabled=True, merchant_id=merchant_id)
            )
            async with runtime.begin():
                return await service.consume_verified_observation(
                    merchant_id, replace(observation, source_kind=source_kind)
                )

    poll, webhook = await asyncio.gather(consume("provider_poll"), consume("provider_webhook"))
    assert poll.id == webhook.id
    async with owner_session_factory() as verify:
        assert await counts(verify, merchant_id) == (1, 1, 1, 1, 1, 2, 1, 1)


@pytest.mark.asyncio
async def test_runtime_outer_rollback_after_receipt_leaves_no_projection_and_caller_recovers(
    owner_session_factory,
    runtime_app_session_factory,
):
    async with owner_session_factory() as owner:
        merchant, invoice, _, mapping = await seed(owner, "runtime-rollback")
        await owner.commit()
        merchant_id, mapping_id, invoice_id = merchant.id, mapping.id, invoice.id
        reusable_name = f"runtime session reusable after rollback {uuid.uuid4()}"
        observation = await ProviderPaymentIngestionService(
            owner, settings=settings(enabled=True, merchant_id=merchant_id)
        ).verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(binding(merchant, mapping))
        )
        assert observation is not None

    async with runtime_app_session_factory() as runtime:
        service = ProviderPaymentIngestionService(
            runtime, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        with pytest.raises(RuntimeError, match="rollback receipt"):
            async with runtime.begin():
                await service.consume_verified_observation(merchant_id, observation)
                raise RuntimeError("rollback receipt")
        # This verifies a clean session after outer rollback, not preservation
        # of unrelated pending work in the same aborted transaction.
        async with runtime.begin():
            runtime.add(
                Merchant(
                    name=reusable_name,
                    email=f"usable-{uuid.uuid4()}@test.invalid",
                )
            )

    async with owner_session_factory() as verify:
        assert await counts(verify, merchant_id) == (0, 0, 0, 0, 0, 0, 0, 0)
        invoice = await verify.scalar(select(Invoice).where(Invoice.id == invoice_id))
        assert invoice.status is InvoiceStatus.AWAITING_PAYMENT
        assert (
            await verify.scalar(
                select(func.count()).select_from(Merchant).where(Merchant.name == reusable_name)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_postreceipt_delivery_changes_do_not_invalidate_duplicate_but_event_mutation_is_rejected(
    owner_session_factory,
):
    async with owner_session_factory() as session:
        merchant, _, _, mapping = await seed(session, "continuity")
        await session.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        service = ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        observation = await service.verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(binding(merchant, mapping))
        )
        assert observation is not None
        await session.rollback()
        async with session.begin():
            fact = await service.consume_verified_observation(merchant_id, observation)
        receipt = await session.scalar(
            select(ProviderPaymentReceipt).where(ProviderPaymentReceipt.observation_id == fact.id)
        )
        intent = await session.scalar(
            select(ProviderPaymentRecipientIntent).where(
                ProviderPaymentRecipientIntent.receipt_id == receipt.id
            )
        )
        assert receipt is not None and intent is not None
        receipt_id, event_id, outbox_id, webhook_id = (
            receipt.id,
            receipt.invoice_event_id,
            intent.outbox_id,
            intent.webhook_id,
        )
        await session.rollback()
        async with session.begin():
            await session.execute(
                text("UPDATE outbox_webhooks SET status='sent' WHERE id=:id"), {"id": outbox_id}
            )
        async with session.begin():
            sent_repeat = await service.consume_verified_observation(
                merchant_id, replace(observation, source_kind="provider_webhook")
            )
        assert sent_repeat.id == fact.id
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE webhooks SET url='https://changed.example.test/callback', is_active=false WHERE id=:id"
                ),
                {"id": webhook_id},
            )
        async with session.begin():
            changed_repeat = await service.consume_verified_observation(merchant_id, observation)
        assert changed_repeat.id == fact.id
        async with session.begin():
            await session.execute(
                text("DELETE FROM outbox_webhooks WHERE id=:id"), {"id": outbox_id}
            )
        async with session.begin():
            deleted_outbox_repeat = await service.consume_verified_observation(
                merchant_id, observation
            )
        assert deleted_outbox_repeat.id == fact.id
        async with session.begin():
            await session.execute(text("DELETE FROM webhooks WHERE id=:id"), {"id": webhook_id})
        async with session.begin():
            deleted_webhook_repeat = await service.consume_verified_observation(
                merchant_id, observation
            )
        assert deleted_webhook_repeat.id == fact.id
        async with session.begin():
            with pytest.raises(DBAPIError) as update_exc:
                await session.execute(
                    text("UPDATE invoice_events SET payload='{}'::json WHERE id=:id"),
                    {"id": event_id},
                )
            assert getattr(update_exc.value.orig, "sqlstate", None) == "P0001"
            assert "referenced by provider receipt is immutable" in str(update_exc.value.orig)
        await session.rollback()
        async with session.begin():
            with pytest.raises(DBAPIError) as delete_exc:
                await session.execute(
                    text("DELETE FROM invoice_events WHERE id=:id"), {"id": event_id}
                )
            assert getattr(delete_exc.value.orig, "sqlstate", None) == "P0001"
            assert "referenced by provider receipt is immutable" in str(delete_exc.value.orig)
        await session.rollback()
        assert receipt_id
        assert await counts(session, merchant_id) == (1, 1, 1, 1, 1, 2, 1, 0)


@pytest.mark.asyncio
async def test_direct_sql_recipient_requires_exact_receipt_and_posting_time_url(
    owner_session_factory,
):
    async with owner_session_factory() as session:
        merchant, _, _, mapping = await seed(session, "intent-guard")
        await session.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        observation = await ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        ).verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(binding(merchant, mapping))
        )
        await session.rollback()
        async with session.begin():
            await ProviderPaymentIngestionService(
                session, settings=settings(enabled=True, merchant_id=merchant_id)
            ).consume_verified_observation(merchant_id, observation)
        receipt = await session.scalar(
            select(ProviderPaymentReceipt).where(ProviderPaymentReceipt.merchant_id == merchant_id)
        )
        intent = await session.scalar(
            select(ProviderPaymentRecipientIntent).where(
                ProviderPaymentRecipientIntent.receipt_id == receipt.id
            )
        )
        receipt_id, intent_outbox_id, intent_webhook_id = (
            receipt.id,
            intent.outbox_id,
            intent.webhook_id,
        )
        intent_payload_snapshot, intent_payload_digest = (
            intent.payload_snapshot,
            intent.payload_digest,
        )
        await session.rollback()
        async with session.begin():
            with pytest.raises(DBAPIError) as exc:
                await session.execute(
                    text(
                        "INSERT INTO provider_payment_recipient_intents "
                        "(id,merchant_id,receipt_id,outbox_id,webhook_id,destination_url,payload_snapshot,destination_digest,payload_digest) "
                        "VALUES (:id,:merchant,:receipt,:outbox,:webhook,:url,CAST(:payload AS jsonb),:destination_digest,:payload_digest)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "merchant": str(merchant_id),
                        "receipt": str(receipt_id),
                        "outbox": str(intent_outbox_id),
                        "webhook": str(intent_webhook_id),
                        "url": "https://attacker.invalid/callback",
                        "payload": json.dumps(intent_payload_snapshot),
                        "destination_digest": hashlib.sha256(
                            b"https://attacker.invalid/callback"
                        ).hexdigest(),
                        "payload_digest": intent_payload_digest,
                    },
                )
            assert getattr(exc.value.orig, "sqlstate", None) == "P0001"
            assert "exact receipt outbox webhook event and snapshot" in str(exc.value.orig)
        await session.rollback()


@pytest.mark.asyncio
async def test_direct_sql_recipient_rejects_existing_same_and_cross_merchant_receipt_substitution(
    owner_session_factory,
):
    async with owner_session_factory() as session:
        merchant_a, _, _, mapping_a = await seed(session, "receipt-a")
        merchant_b, _, _, mapping_b = await seed(session, "receipt-b", merchant=merchant_a)
        merchant_c, _, _, mapping_c = await seed(session, "receipt-c")
        await session.commit()

        merchant_a_id, mapping_a_id, binding_a = (
            merchant_a.id,
            mapping_a.id,
            binding(merchant_a, mapping_a),
        )
        merchant_b_id, mapping_b_id, binding_b = (
            merchant_a.id,
            mapping_b.id,
            binding(merchant_b, mapping_b),
        )
        merchant_c_id, mapping_c_id, binding_c = (
            merchant_c.id,
            mapping_c.id,
            binding(merchant_c, mapping_c),
        )

        async def project(merchant_id, mapping_id, trusted_binding):
            service = ProviderPaymentIngestionService(
                session, settings=settings(enabled=True, merchant_id=merchant_id)
            )
            observation = await service.verify_provider_invoice(
                merchant_id, mapping_id, TrustedFakeVerifier(trusted_binding)
            )
            await session.rollback()
            async with session.begin():
                await service.consume_verified_observation(merchant_id, observation)
            return observation

        await project(merchant_a_id, mapping_a_id, binding_a)
        await project(merchant_b_id, mapping_b_id, binding_b)
        await project(merchant_c_id, mapping_c_id, binding_c)
        receipt_a = await session.scalar(
            select(ProviderPaymentReceipt)
            .join(ProviderPaymentObservation)
            .where(ProviderPaymentObservation.mapping_id == mapping_a_id)
        )
        receipt_b = await session.scalar(
            select(ProviderPaymentReceipt)
            .join(ProviderPaymentObservation)
            .where(ProviderPaymentObservation.mapping_id == mapping_b_id)
        )
        receipt_c = await session.scalar(
            select(ProviderPaymentReceipt)
            .join(ProviderPaymentObservation)
            .where(ProviderPaymentObservation.mapping_id == mapping_c_id)
        )
        assert receipt_a is not None and receipt_b is not None and receipt_c is not None
        intent_a = await session.scalar(
            select(ProviderPaymentRecipientIntent).where(
                ProviderPaymentRecipientIntent.receipt_id == receipt_a.id
            )
        )
        intent_b = await session.scalar(
            select(ProviderPaymentRecipientIntent).where(
                ProviderPaymentRecipientIntent.receipt_id == receipt_b.id
            )
        )
        assert intent_a is not None and intent_b is not None
        values = {
            "id": str(uuid.uuid4()),
            "merchant": str(merchant_a_id),
            "receipt": str(receipt_a.id),
            "outbox": str(intent_b.outbox_id),
            "webhook": str(intent_b.webhook_id),
            "url": intent_b.destination_url,
            "payload": json.dumps(intent_b.payload_snapshot),
            "destination_digest": intent_b.destination_digest,
            "payload_digest": intent_b.payload_digest,
        }
        receipt_c_id = receipt_c.id
        intent_a_id = intent_a.id
        await session.rollback()
        async with session.begin():
            with pytest.raises(DBAPIError) as same_exc:
                await session.execute(
                    text(
                        "INSERT INTO provider_payment_recipient_intents "
                        "(id,merchant_id,receipt_id,outbox_id,webhook_id,destination_url,payload_snapshot,destination_digest,payload_digest) "
                        "VALUES (:id,:merchant,:receipt,:outbox,:webhook,:url,CAST(:payload AS jsonb),:destination_digest,:payload_digest)"
                    ),
                    values,
                )
            assert getattr(same_exc.value.orig, "sqlstate", None) == "P0001"
            assert "exact receipt outbox webhook event and snapshot" in str(same_exc.value.orig)
        await session.rollback()
        values["id"] = str(uuid.uuid4())
        values["receipt"] = str(receipt_c_id)
        async with session.begin():
            with pytest.raises(DBAPIError) as cross_exc:
                await session.execute(
                    text(
                        "INSERT INTO provider_payment_recipient_intents "
                        "(id,merchant_id,receipt_id,outbox_id,webhook_id,destination_url,payload_snapshot,destination_digest,payload_digest) "
                        "VALUES (:id,:merchant,:receipt,:outbox,:webhook,:url,CAST(:payload AS jsonb),:destination_digest,:payload_digest)"
                    ),
                    values,
                )
            assert getattr(cross_exc.value.orig, "sqlstate", None) == "P0001"
            assert "exact receipt outbox webhook event and snapshot" in str(cross_exc.value.orig)
        await session.rollback()
        assert intent_a_id


@pytest.mark.asyncio
async def test_wrong_id_asset_amount_and_verifier_binding_are_reconciliation_failures(
    owner_session_factory,
):
    async with rollback_session(owner_session_factory) as session:
        merchant, _, _, mapping = await seed(session, "reject")
        await session.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        trusted_binding = binding(merchant, mapping)
        service = ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        bad_binding = replace(trusted_binding, provider_account_identity=sha("different-account"))
        for verifier in (
            TrustedFakeVerifier(bad_binding),
            TrustedFakeVerifier(trusted_binding, provider_invoice_id="different-id"),
            TrustedFakeVerifier(trusted_binding, asset="USDC"),
        ):
            with pytest.raises(ProviderReconciliationConflict):
                await service.verify_provider_invoice(merchant_id, mapping_id, verifier)
        observation = await service.verify_provider_invoice(
            merchant_id,
            mapping_id,
            TrustedFakeVerifier(trusted_binding, amount=Decimal("12.33")),
        )
        assert observation is not None
        await session.rollback()
        async with session.begin():
            with pytest.raises(ProviderReconciliationConflict):
                await service.consume_verified_observation(merchant_id, observation)
        assert await counts(session, merchant_id) == (0, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_direct_preexisting_observation_without_receipt_is_reconciliation_not_success(
    owner_session_factory,
):
    async with rollback_session(owner_session_factory) as session:
        merchant, _, _, mapping = await seed(session, "partial-proof")
        await session.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        trusted_binding = binding(merchant, mapping)
        rail_id = mapping.rail_id
        provider_account_identity = mapping.provider_account_identity
        provider_network = mapping.provider_network
        provider_invoice_id = mapping.provider_invoice_id
        ledger_asset_id = mapping.ledger_asset_id
        service = ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        observation = await service.verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(trusted_binding)
        )
        assert observation is not None
        await session.rollback()
        async with session.begin():
            session.add(
                ProviderPaymentObservation(
                    merchant_id=merchant_id,
                    mapping_id=mapping_id,
                    rail_id=rail_id,
                    provider_account_identity=provider_account_identity,
                    provider_network=provider_network,
                    provider_invoice_id=provider_invoice_id,
                    ledger_asset_id=ledger_asset_id,
                    observed_amount_atomic=observation.observed_amount_atomic,
                    observed_at=datetime.now(UTC),
                    source_kind="provider_poll",
                    economic_digest=observation.economic_digest,
                    evidence_digest=observation.evidence_digest,
                )
            )
        async with session.begin():
            with pytest.raises(ProviderReconciliationConflict, match="receipt proof"):
                await service.consume_verified_observation(merchant_id, observation)
        assert await counts(session, merchant_id) == (1, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_envelope_rejects_bool_amount_cross_actor_and_cross_account_replay(
    owner_session_factory,
):
    async with rollback_session(owner_session_factory) as session:
        merchant, _, _, mapping = await seed(session, "tenant-a")
        other, _, _, other_mapping = await seed(session, "tenant-b")
        await session.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        other_merchant_id, other_mapping_id = other.id, other_mapping.id
        trusted_binding = binding(merchant, mapping)
        service = ProviderPaymentIngestionService(
            session,
            settings=Settings(
                secret_key="x" * 32,
                encryption_key="x" * 32,
                ledger_rail_orchestration_enabled=True,
                ledger_rail_orchestration_merchants=f"{merchant_id},{other_merchant_id}",
            ),
        )
        observation = await service.verify_provider_invoice(
            merchant_id, mapping_id, TrustedFakeVerifier(trusted_binding)
        )
        assert observation is not None
        await session.rollback()
        async with session.begin():
            with pytest.raises(ProviderReconciliationConflict):
                await service.consume_verified_observation(other_merchant_id, observation)
            with pytest.raises(ProviderReconciliationConflict):
                await service.consume_verified_observation(
                    merchant_id, replace(observation, mapping_id=other_mapping_id)
                )
            with pytest.raises(Exception, match="observed atomic amount must be an int"):
                await service.consume_verified_observation(
                    merchant_id, replace(observation, observed_amount_atomic=True)
                )


@pytest.mark.asyncio
async def test_harmless_metadata_and_poll_webhook_source_variation_are_idempotent(
    owner_session_factory,
):
    async with rollback_session(owner_session_factory) as session:
        merchant, _, _, mapping = await seed(session, "semantic")
        await session.commit()
        merchant_id, mapping_id = merchant.id, mapping.id
        trusted_binding = binding(merchant, mapping)
        service = ProviderPaymentIngestionService(
            session, settings=settings(enabled=True, merchant_id=merchant_id)
        )
        first = await service.verify_provider_invoice(
            merchant_id,
            mapping_id,
            TrustedFakeVerifier(trusted_binding, raw={"status": "paid", "retrieval": "one"}),
        )
        second = await service.verify_provider_invoice(
            merchant_id,
            mapping_id,
            TrustedFakeVerifier(trusted_binding, raw={"status": "paid", "retrieval": "two"}),
        )
        assert first is not None and second is not None
        assert first.economic_digest == second.economic_digest
        assert first.evidence_digest != second.evidence_digest

        third = await service.verify_provider_invoice(
            merchant_id,
            mapping_id,
            TrustedFakeVerifier(
                trusted_binding,
                paid_at="2026-09-05T03:00:00+03:00",
                raw={"status": "paid", "retrieval": "three"},
            ),
        )
        assert third is not None
        assert third.economic_digest == first.economic_digest
        await session.rollback()
        async with session.begin():
            fact = await service.consume_verified_observation(merchant_id, first)
        async with session.begin():
            repeated = await service.consume_verified_observation(
                merchant_id, replace(second, source_kind="provider_webhook")
            )
        assert repeated.id == fact.id
        async with session.begin():
            timestamp_variant = await service.consume_verified_observation(
                merchant_id, replace(third, source_kind="provider_webhook")
            )
        assert timestamp_variant.id == fact.id
        assert await counts(session, merchant_id) == (1, 1, 1, 1, 1, 2, 1, 1)
