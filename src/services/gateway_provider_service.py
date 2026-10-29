"""Custodial invoice lifecycle: persist once, call outside SQL, reconcile without resubmission."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import Settings, get_settings
from src.core.exceptions import AppException
from src.crypto.encryption import decrypt_secret
from src.db.models import (
    GatewayInvoiceOptions,
    GatewayLiquidityRecognition,
    GatewayOperation,
    GatewayPayoutReceipt,
    GatewayRailConnection,
    Invoice,
    InvoiceStatus,
    LedgerAccount,
    LedgerAsset,
    LedgerEntry,
    LedgerTransaction,
    Merchant,
    PaymentSession,
    ProviderPaymentMapping,
    ProviderPaymentObservation,
    ProviderPaymentReceipt,
    ProviderSettlement,
)
from src.db.models import (
    Rail as RailModel,
)
from src.db.models.ledger import LedgerDirection
from src.ledger.amounts import atomic_to_decimal, decimal_to_atomic
from src.payments.policy import CUSTODIAL_RAIL_TYPES, asset_decimals
from src.payments.rails.base import (
    Rail,
    RailError,
    RailInvoice,
    RailInvoiceRequest,
    RailInvoiceStatus,
    RailPayout,
    RailPayoutRequest,
)
from src.payments.rails.registry import build_rail, encrypt_credentials
from src.services.ledger_posting_service import LedgerLine, LedgerPostingService
from src.services.provider_payment_ingestion import (
    ProviderPaymentIngestionService,
    ProviderReconciliationConflict,
    ProviderVerificationBinding,
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def credentials_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def account_digest(rail_type: str, network: str, account: str) -> str:
    return digest({"provider": rail_type, "network": network, "account": account})


# Clock skew allowance before asking the provider whether an invoice expired.
PROVIDER_EXPIRY_GRACE = timedelta(minutes=2)


class GatewayError(AppException):
    def __init__(self, message: str, *, code: str = "GATEWAY_CONFLICT", status_code: int = 409):
        super().__init__(message=message, code=code, status_code=status_code)


@dataclass(frozen=True)
class ConnectionSnapshot:
    merchant_id: UUID
    rail_id: UUID
    rail_type: str
    network: str
    account_identity: str
    encrypted_credentials: str


@dataclass(frozen=True)
class OperationResult:
    id: UUID
    status: str
    rail_id: UUID
    provider_reference: str | None
    pay_url: str | None
    failure_code: str | None
    operation_type: str
    ledger_asset_id: UUID
    amount_atomic: str

    @classmethod
    def from_row(cls, row: GatewayOperation) -> OperationResult:
        return cls(
            row.id,
            row.status,
            row.rail_id,
            row.provider_reference,
            row.pay_url,
            row.failure_code,
            row.operation_type,
            row.ledger_asset_id,
            str(int(row.amount_atomic)),
        )


class BoundVerifier:
    """Binds a server-created adapter to an immutable connection, not a client paid flag."""

    def __init__(
        self, rail: Rail, binding: ProviderVerificationBinding, snapshot: ConnectionSnapshot
    ):
        self.rail, self.binding, self.snapshot = rail, binding, snapshot

    async def verify_payment(self, provider_invoice_id: str):
        actual = account_digest(
            self.snapshot.rail_type, self.snapshot.network, await self.rail.account_identity()
        )
        if not hmac.compare_digest(actual, self.binding.provider_account_identity):
            raise ProviderReconciliationConflict("provider application identity changed")
        return await self.rail.verify_payment(provider_invoice_id)


class GatewayProviderService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        rail_builder: Callable[..., Rail] = build_rail,
    ):
        self.factory = factory
        self.settings = settings or get_settings()
        self.rail_builder = rail_builder

    def require_enabled(self, merchant_id: UUID) -> None:
        if (
            not self.settings.ledger_rail_orchestration_enabled
            or str(merchant_id) not in self.settings.ledger_rail_orchestration_merchant_ids
        ):
            raise GatewayError(
                "Provider payments are unavailable", code="GATEWAY_DISABLED", status_code=503
            )

    def _build(self, snapshot: ConnectionSnapshot) -> Rail:
        return self.rail_builder(
            snapshot.rail_type, snapshot.encrypted_credentials, network=snapshot.network
        )

    async def _connection(
        self, session: AsyncSession, merchant_id: UUID, rail_id: UUID
    ) -> tuple[ConnectionSnapshot, GatewayRailConnection]:
        connection = await session.scalar(
            select(GatewayRailConnection).where(
                GatewayRailConnection.rail_id == rail_id,
                GatewayRailConnection.merchant_id == merchant_id,
            )
        )
        rail = await session.scalar(
            select(RailModel).where(
                RailModel.id == rail_id,
                RailModel.merchant_id == merchant_id,
                RailModel.is_active.is_(True),
            )
        )
        if connection is None or rail is None or rail.encrypted_credentials is None:
            raise GatewayError("Payment method is unavailable", status_code=404)
        merchant = await session.get(Merchant, merchant_id)
        if merchant is None or not merchant.is_active:
            raise GatewayError("Merchant is unavailable", status_code=404)
        if (
            rail.rail_type != connection.rail_type
            or rail.network != connection.network
            or credentials_digest(rail.encrypted_credentials) != connection.credentials_digest
        ):
            raise GatewayError("Payment method needs account reconciliation")
        return ConnectionSnapshot(
            merchant_id,
            rail_id,
            rail.rail_type,
            connection.network,
            connection.account_identity,
            rail.encrypted_credentials,
        ), connection

    async def connect(
        self,
        merchant_id: UUID,
        rail_type: str,
        token: str,
        assets: list[str],
        *,
        network: str = "MAIN_NET",
        label: str | None = None,
    ) -> UUID:
        self.require_enabled(merchant_id)
        if rail_type not in CUSTODIAL_RAIL_TYPES or not assets or len(set(assets)) != len(assets):
            raise GatewayError("Invalid provider configuration", status_code=422)
        network = network.upper()
        for symbol in assets:
            asset_decimals(rail_type, symbol)
        encrypted = encrypt_credentials({"token": token})
        adapter = self.rail_builder(rail_type, encrypted, network=network)
        try:
            if any(symbol not in adapter.supported_assets() for symbol in assets):
                raise GatewayError("Unsupported provider asset", status_code=422)
            identity = account_digest(rail_type, network, await adapter.account_identity())
        finally:
            await adapter.close()
        async with self.factory() as session, session.begin():
            merchant = await session.get(Merchant, merchant_id)
            if merchant is None or not merchant.is_active:
                raise GatewayError("Merchant is unavailable", status_code=404)
            existing = await session.scalar(
                select(GatewayRailConnection).where(
                    GatewayRailConnection.rail_type == rail_type,
                    GatewayRailConnection.network == network,
                    GatewayRailConnection.account_identity == identity,
                )
            )
            if existing is not None:
                if existing.merchant_id != merchant_id:
                    raise GatewayError("Provider application is already connected")
                old_rail = await session.get(RailModel, existing.rail_id)
                old_credentials = json.loads(
                    decrypt_secret(
                        bytes.fromhex(old_rail.encrypted_credentials),
                        self.settings.encryption_key.get_secret_value(),
                    )
                )
                if not hmac.compare_digest(old_credentials["token"], token):
                    raise GatewayError(
                        "Provider credential rotation requires account reconciliation"
                    )
                if old_rail.assets != assets:
                    raise GatewayError(
                        "Provider asset configuration differs from existing connection"
                    )
                return existing.rail_id
            rail = RailModel(
                id=uuid4(),
                merchant_id=merchant_id,
                rail_type=rail_type,
                network=network,
                encrypted_credentials=encrypted,
                assets=assets,
                label=label,
                is_active=True,
            )
            receivable = LedgerAccount(
                id=uuid4(),
                merchant_id=merchant_id,
                account_type="merchant_custodial_receivable",
                custody_type="merchant_custodial",
            )
            payable = LedgerAccount(
                id=uuid4(),
                merchant_id=merchant_id,
                account_type="merchant_payable",
                custody_type="merchant_liability",
            )
            session.add_all([rail, receivable, payable])
            await session.flush()
            session.add(
                GatewayRailConnection(
                    rail_id=rail.id,
                    merchant_id=merchant_id,
                    rail_type=rail_type,
                    network=network,
                    account_identity=identity,
                    credentials_digest=credentials_digest(encrypted),
                    receivable_account_id=receivable.id,
                    payable_account_id=payable.id,
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                raise GatewayError("Provider application is already connected") from exc
            return rail.id

    async def list_methods(self, merchant_id: UUID) -> list[dict]:
        self.require_enabled(merchant_id)
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(RailModel)
                    .join(GatewayRailConnection, GatewayRailConnection.rail_id == RailModel.id)
                    .where(RailModel.merchant_id == merchant_id, RailModel.is_active.is_(True))
                )
            ).all()
            snapshots = []
            for row in rows:
                snapshot, _ = await self._connection(session, merchant_id, row.id)
                snapshots.append((snapshot, list(row.assets), row.label))
        result = []
        for snapshot, assets, label in snapshots:
            adapter = self._build(snapshot)
            try:
                from dataclasses import asdict

                result.append(
                    {
                        "id": str(snapshot.rail_id),
                        "type": snapshot.rail_type,
                        "network": snapshot.network,
                        "assets": assets,
                        "label": label,
                        "capabilities": asdict(adapter.capabilities),
                    }
                )
            finally:
                await adapter.close()
        return result

    async def _asset(
        self, session: AsyncSession, rail_type: str, network: str, symbol: str
    ) -> LedgerAsset:
        decimals = asset_decimals(rail_type, symbol)
        asset = await session.scalar(
            select(LedgerAsset).where(
                LedgerAsset.network_kind == "custodial",
                LedgerAsset.network_identifier == f"{rail_type}:{network}",
                LedgerAsset.canonical_identifier == symbol,
                LedgerAsset.is_native.is_(False),
            )
        )
        if asset is None:
            asset = LedgerAsset(
                id=uuid4(),
                network_kind="custodial",
                network_identifier=f"{rail_type}:{network}",
                canonical_identifier=symbol,
                is_native=False,
                atomic_decimals=decimals,
                symbol=symbol,
            )
            # A network-scoped registry identity can be created concurrently.
            try:
                async with session.begin_nested():
                    session.add(asset)
                    await session.flush()
            except IntegrityError:
                asset = await session.scalar(
                    select(LedgerAsset).where(
                        LedgerAsset.network_kind == "custodial",
                        LedgerAsset.network_identifier == f"{rail_type}:{network}",
                        LedgerAsset.canonical_identifier == symbol,
                        LedgerAsset.is_native.is_(False),
                    )
                )
        if asset is None or asset.atomic_decimals != decimals:
            raise GatewayError("Provider asset precision requires reconciliation")
        return asset

    async def select_invoice(self, public_id: str, rail_id: UUID) -> OperationResult:
        async with self.factory() as session, session.begin():
            invoice = await session.scalar(
                select(Invoice).where(Invoice.public_id == public_id).with_for_update()
            )
            if invoice is None:
                raise GatewayError("Invoice not found", status_code=404)
            merchant_id = invoice.merchant_id
            self.require_enabled(merchant_id)
            previous = await session.scalar(
                select(GatewayOperation).where(GatewayOperation.invoice_id == invoice.id)
            )
            if previous is not None:
                if previous.rail_id != rail_id:
                    raise GatewayError("Invoice already has a selected payment method")
                return OperationResult.from_row(previous)
            if not invoice.is_payable:
                raise GatewayError("Invoice is no longer payable", status_code=410)
            options = await session.get(GatewayInvoiceOptions, invoice.id)
            if options is None or str(rail_id) not in options.allowed_rails:
                raise GatewayError(
                    "Payment method is not allowed for this invoice", status_code=422
                )
            if await session.scalar(
                select(PaymentSession.id).where(PaymentSession.invoice_id == invoice.id).limit(1)
            ):
                raise GatewayError("Invoice already has an on-chain payment method")
            snapshot, connection = await self._connection(session, merchant_id, rail_id)
            rail = await session.get(RailModel, rail_id)
            if invoice.asset not in rail.assets:
                raise GatewayError("Asset is unavailable for this method", status_code=422)
            asset = await self._asset(session, snapshot.rail_type, snapshot.network, invoice.asset)
            amount_atomic = decimal_to_atomic(invoice.amount, asset.atomic_decimals)
            request_identity = {
                "invoice": str(invoice.id),
                "rail": str(rail_id),
                "asset": str(asset.id),
                "amount": str(amount_atomic),
                "customer": options.customer_telegram_id,
            }
            operation = GatewayOperation(
                id=uuid4(),
                merchant_id=merchant_id,
                rail_id=rail_id,
                operation_type="invoice",
                invoice_id=invoice.id,
                ledger_asset_id=asset.id,
                amount_atomic=amount_atomic,
                destination="",
                idempotency_key=public_id,
                request_digest=digest(request_identity),
                status="submitting",
            )
            session.add(operation)
            invoice.status = InvoiceStatus.AWAITING_PAYMENT
            await session.flush()
            operation_id = operation.id
            request = RailInvoiceRequest(
                amount=invoice.amount,
                asset=invoice.asset,
                description=f"Payment {invoice.public_id}",
                external_id=str(operation_id),
                expires_in_minutes=max(
                    1, int((invoice.expires_at - datetime.now(UTC)).total_seconds() / 60)
                ),
                customer_telegram_id=options.customer_telegram_id,
            )
        # The operation is durable and all SQL locks are released before I/O.
        adapter = self._build(snapshot)
        try:
            provider_invoice = await adapter.create_invoice(request)
        except RailError as exc:
            return await self._creation_failed(operation_id, merchant_id, exc)
        finally:
            await adapter.close()
        return await self._bind_invoice(operation_id, merchant_id, provider_invoice)

    async def _creation_failed(
        self, operation_id: UUID, merchant_id: UUID, error: RailError
    ) -> OperationResult:
        async with self.factory() as session, session.begin():
            operation = await session.scalar(
                select(GatewayOperation)
                .where(
                    GatewayOperation.id == operation_id, GatewayOperation.merchant_id == merchant_id
                )
                .with_for_update()
            )
            if operation is None:
                raise GatewayError("Operation not found", status_code=404)
            if operation.status not in {"submitting", "unknown"}:
                return OperationResult.from_row(operation)
            operation.status = "unknown" if error.outcome_unknown else "failed"
            operation.failure_code = (
                "provider_outcome_unknown" if error.outcome_unknown else "provider_rejected"
            )
            await session.flush()
            return OperationResult.from_row(operation)

    async def _bind_invoice(
        self, operation_id: UUID, merchant_id: UUID, provider_invoice: RailInvoice
    ) -> OperationResult:
        async with self.factory() as session, session.begin():
            operation = await session.scalar(
                select(GatewayOperation)
                .where(
                    GatewayOperation.id == operation_id, GatewayOperation.merchant_id == merchant_id
                )
                .with_for_update()
            )
            if operation is None:
                raise GatewayError("Operation not found", status_code=404)
            if operation.status not in {"submitting", "unknown"}:
                return OperationResult.from_row(operation)
            snapshot, connection = await self._connection(session, merchant_id, operation.rail_id)
            asset = await session.get(LedgerAsset, operation.ledger_asset_id)
            invoice = await session.scalar(
                select(Invoice).where(Invoice.id == operation.invoice_id).with_for_update()
            )
            try:
                returned_amount = decimal_to_atomic(provider_invoice.amount, asset.atomic_decimals)
            except ValueError:
                returned_amount = None
            if (
                invoice is None
                or not invoice.is_payable
                or provider_invoice.asset != asset.symbol
                or returned_amount != int(operation.amount_atomic)
                or provider_invoice.status
                not in {
                    RailInvoiceStatus.CREATED,
                    RailInvoiceStatus.PENDING,
                    RailInvoiceStatus.PAID,
                }
            ):
                operation.status, operation.failure_code = (
                    "reconciliation_required",
                    "provider_invoice_binding_conflict",
                )
                operation.provider_reference = provider_invoice.provider_invoice_id
                await session.flush()
                return OperationResult.from_row(operation)
            mapping = ProviderPaymentMapping(
                id=uuid4(),
                merchant_id=merchant_id,
                invoice_id=invoice.id,
                rail_id=operation.rail_id,
                provider_account_identity=connection.account_identity,
                provider_network=connection.network,
                provider_invoice_id=provider_invoice.provider_invoice_id,
                provider_asset_code=asset.symbol,
                ledger_asset_id=asset.id,
                expected_amount_atomic=operation.amount_atomic,
                receivable_account_id=connection.receivable_account_id,
                payable_account_id=connection.payable_account_id,
            )
            session.add(mapping)
            await session.flush()
            operation.mapping_id, operation.provider_reference = (
                mapping.id,
                provider_invoice.provider_invoice_id,
            )
            operation.pay_url, operation.status = provider_invoice.pay_url, "pending"
            operation.failure_code = None
            await session.flush()
            return OperationResult.from_row(operation)

    async def get_operation(self, merchant_id: UUID, operation_id: UUID) -> OperationResult:
        self.require_enabled(merchant_id)
        async with self.factory() as session:
            row = await session.scalar(
                select(GatewayOperation).where(
                    GatewayOperation.id == operation_id, GatewayOperation.merchant_id == merchant_id
                )
            )
            if row is None:
                raise GatewayError("Operation not found", status_code=404)
            return OperationResult.from_row(row)

    async def receipt(self, merchant_id: UUID, invoice_id: UUID) -> dict:
        self.require_enabled(merchant_id)
        async with self.factory() as session:
            invoice = await session.scalar(
                select(Invoice).where(Invoice.id == invoice_id, Invoice.merchant_id == merchant_id)
            )
            if invoice is None:
                raise GatewayError("Invoice not found", status_code=404)
            record = (
                await session.execute(
                    select(
                        ProviderPaymentReceipt,
                        ProviderPaymentObservation,
                        ProviderSettlement,
                        LedgerAsset,
                    )
                    .join(
                        ProviderPaymentObservation,
                        ProviderPaymentObservation.id == ProviderPaymentReceipt.observation_id,
                    )
                    .join(
                        ProviderSettlement,
                        ProviderSettlement.observation_id == ProviderPaymentObservation.id,
                    )
                    .join(LedgerAsset, LedgerAsset.id == ProviderPaymentObservation.ledger_asset_id)
                    .where(
                        ProviderPaymentReceipt.merchant_id == merchant_id,
                        ProviderPaymentReceipt.invoice_id == invoice_id,
                    )
                )
            ).one_or_none()
            if record is None and get_settings().onchain_gateway_enabled:
                from src.db.models import OnchainReceipt

                chain_record = await session.scalar(
                    select(OnchainReceipt).where(
                        OnchainReceipt.merchant_id == merchant_id,
                        OnchainReceipt.invoice_id == invoice_id,
                    )
                )
                if chain_record:
                    asset = await session.get(LedgerAsset, chain_record.asset_id)
                    value = atomic_to_decimal(
                        int(chain_record.amount_atomic), asset.atomic_decimals
                    )
                    return {
                        "invoice_id": str(invoice_id),
                        "status": invoice.status.value,
                        "receipt": {
                            "id": str(chain_record.id),
                            "event_id": chain_record.event_id,
                            "asset": asset.symbol,
                            "network": asset.network_identifier,
                            "gross": value,
                            "net": value,
                            "provider_fee": Decimal(0),
                        },
                    }
            if record is None:
                return {
                    "invoice_id": str(invoice_id),
                    "status": invoice.status.value,
                    "receipt": None,
                }
            receipt, observation, settlement, asset = record
            gross = int(observation.observed_amount_atomic)
            net = int(settlement.settled_amount_atomic)
            return {
                "invoice_id": str(invoice_id),
                "status": invoice.status.value,
                "receipt": {
                    "id": str(receipt.id),
                    "event_id": receipt.event_id,
                    "asset": asset.symbol,
                    "network": asset.network_identifier,
                    "gross": atomic_to_decimal(gross, asset.atomic_decimals),
                    "net": atomic_to_decimal(net, asset.atomic_decimals),
                    "provider_fee": atomic_to_decimal(gross - net, asset.atomic_decimals),
                },
            }

    async def handle_provider_webhook(
        self, rail_id: UUID, body: bytes, headers: Mapping[str, str]
    ) -> OperationResult | None:
        """Authenticate a provider push and verify the named invoice immediately.

        The push is only a wake-up: payment facts come from poll_invoice, and the
        poller remains the fallback for lost or rejected deliveries.
        """
        async with self.factory() as session:
            merchant_id = await session.scalar(
                select(GatewayRailConnection.merchant_id).where(
                    GatewayRailConnection.rail_id == rail_id
                )
            )
            if merchant_id is None:
                raise GatewayError("Payment method is unavailable", status_code=404)
            self.require_enabled(merchant_id)
            snapshot, _ = await self._connection(session, merchant_id, rail_id)
        adapter = self._build(snapshot)
        try:
            invoice_id = adapter.webhook_invoice_id(
                body, headers, self.settings.provider_webhook_secret_map.get(str(rail_id), "")
            )
        finally:
            await adapter.close()
        if invoice_id is None:
            return None
        async with self.factory() as session:
            operation_id = await session.scalar(
                select(GatewayOperation.id).where(
                    GatewayOperation.merchant_id == merchant_id,
                    GatewayOperation.rail_id == rail_id,
                    GatewayOperation.operation_type == "invoice",
                    GatewayOperation.provider_reference == invoice_id,
                )
            )
        if operation_id is None:
            return None
        return await self.poll_invoice(merchant_id, operation_id)

    async def poll_invoice(self, merchant_id: UUID, operation_id: UUID) -> OperationResult:
        self.require_enabled(merchant_id)
        async with self.factory() as session, session.begin():
            operation = await session.scalar(
                select(GatewayOperation)
                .where(
                    GatewayOperation.id == operation_id,
                    GatewayOperation.merchant_id == merchant_id,
                    GatewayOperation.operation_type == "invoice",
                )
                .with_for_update()
            )
            if operation is None:
                raise GatewayError("Operation not found", status_code=404)
            if operation.status in {"completed", "failed", "reconciliation_required"}:
                return OperationResult.from_row(operation)
            # Keep unpaid or temporarily unreadable invoices from starving
            # later invoices in the bounded worker batch.
            operation.updated_at = datetime.now(UTC)
            await session.flush()
            snapshot, connection = await self._connection(session, merchant_id, operation.rail_id)
            mapping_id, status, created_at, reference = (
                operation.mapping_id,
                operation.status,
                operation.created_at,
                operation.provider_reference,
            )
            payable_account_id = connection.payable_account_id
            expires_at = (await session.get(Invoice, operation.invoice_id)).expires_at
            if mapping_id is not None:
                mapping = await session.get(ProviderPaymentMapping, mapping_id)
                binding = ProviderVerificationBinding(
                    merchant_id,
                    mapping_id,
                    operation.rail_id,
                    connection.account_identity,
                    connection.network,
                    mapping.ledger_asset_id,
                )
        adapter = self._build(snapshot)
        try:
            if mapping_id is None:
                if status == "submitting" and (datetime.now(UTC) - created_at).total_seconds() < 60:
                    return await self.get_operation(merchant_id, operation_id)
                if adapter.capabilities.invoice_lookup_by_client_id:
                    recovered = await adapter.find_invoice(str(operation_id))
                    if recovered is not None:
                        return await self._bind_invoice(operation_id, merchant_id, recovered)
                return await self._creation_failed(
                    operation_id,
                    merchant_id,
                    RailError(
                        snapshot.rail_type, "creation requires reconciliation", outcome_unknown=True
                    ),
                )
            async with self.factory() as session:
                service = ProviderPaymentIngestionService(session, settings=self.settings)
                observation = await service.verify_provider_invoice(
                    merchant_id, mapping_id, BoundVerifier(adapter, binding, snapshot)
                )
                if observation is None:
                    if expires_at + PROVIDER_EXPIRY_GRACE <= datetime.now(UTC):
                        return await self._close_expired(merchant_id, operation_id, adapter, reference)
                    return await self.get_operation(merchant_id, operation_id)
                if observation.settled_amount_atomic is None:
                    # No price-based credit when actual provider fees are unknown.
                    raise ProviderReconciliationConflict("settlement evidence is unavailable")
                async with session.begin():
                    # Same lock as payouts and liquidity recognition: a payout never
                    # sees provider funds that are neither open nor yet in the ledger.
                    await session.scalar(
                        select(LedgerAccount.id)
                        .where(LedgerAccount.id == payable_account_id)
                        .with_for_update()
                    )
                    await service.consume_verified_observation(merchant_id, observation)
                    operation = await session.scalar(
                        select(GatewayOperation)
                        .where(
                            GatewayOperation.id == operation_id,
                            GatewayOperation.merchant_id == merchant_id,
                        )
                        .with_for_update()
                    )
                    if operation.status != "completed":
                        operation.status, operation.failure_code = "completed", None
                        await session.flush()
                    return OperationResult.from_row(operation)
        except ProviderReconciliationConflict:
            async with self.factory() as session, session.begin():
                operation = await session.scalar(
                    select(GatewayOperation)
                    .where(
                        GatewayOperation.id == operation_id,
                        GatewayOperation.merchant_id == merchant_id,
                    )
                    .with_for_update()
                )
                if operation.status in {"pending", "unknown", "submitting"}:
                    operation.status, operation.failure_code = (
                        "reconciliation_required",
                        "verified_payment_conflict",
                    )
                    await session.flush()
                return OperationResult.from_row(operation)
        except RailError:
            # A read failure does not invalidate an already allocated invoice.
            return await self.get_operation(merchant_id, operation_id)
        finally:
            await adapter.close()

    async def _close_expired(self, merchant_id, operation_id, adapter, reference):
        # Provider TTL never exceeds the invoice TTL; only the provider's own
        # EXPIRED verdict ends the operation, so a late paid invoice still credits.
        provider_invoice = await adapter.get_invoice(reference)
        if provider_invoice.status != RailInvoiceStatus.EXPIRED:
            return await self.get_operation(merchant_id, operation_id)
        async with self.factory() as session, session.begin():
            operation = await session.scalar(
                select(GatewayOperation)
                .where(
                    GatewayOperation.id == operation_id,
                    GatewayOperation.merchant_id == merchant_id,
                )
                .with_for_update()
            )
            if operation.status == "pending":
                operation.status, operation.failure_code = "failed", "provider_invoice_expired"
                await session.flush()
            return OperationResult.from_row(operation)

    async def poll_batch(self, limit: int = 50) -> int:
        if not self.settings.ledger_rail_orchestration_enabled:
            return 0
        allowlist = [UUID(value) for value in self.settings.ledger_rail_orchestration_merchant_ids]
        if not allowlist:
            return 0
        async with self.factory() as session:
            rows = (
                await session.execute(
                    select(
                        GatewayOperation.merchant_id,
                        GatewayOperation.id,
                        GatewayOperation.operation_type,
                    )
                    .where(
                        GatewayOperation.status.in_(["pending", "unknown", "submitting"]),
                        GatewayOperation.merchant_id.in_(allowlist),
                    )
                    .order_by(GatewayOperation.updated_at)
                    .limit(limit)
                )
            ).all()
        processed = 0
        for merchant_id, operation_id, operation_type in rows:
            try:
                if operation_type == "invoice":
                    await self.poll_invoice(merchant_id, operation_id)
                else:
                    await self.reconcile_payout(merchant_id, operation_id)
            except GatewayError:
                async with self.factory() as session, session.begin():
                    row = await session.scalar(
                        select(GatewayOperation)
                        .where(GatewayOperation.id == operation_id)
                        .with_for_update()
                    )
                    if row is not None and row.status in {"pending", "submitting", "unknown"}:
                        row.status, row.failure_code = (
                            "reconciliation_required",
                            "connection_reconciliation_required",
                        )
                        await session.flush()
            processed += 1
        return processed

    async def _funds(self, session, merchant_id, connection, asset_id, sampled_at):
        balance = int(
            await session.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (LedgerEntry.direction == "credit", LedgerEntry.amount_atomic),
                                else_=-LedgerEntry.amount_atomic,
                            )
                        ),
                        0,
                    )
                )
                .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.ledger_transaction_id)
                .where(
                    LedgerEntry.merchant_id == merchant_id,
                    LedgerEntry.ledger_account_id == connection.payable_account_id,
                    LedgerEntry.ledger_asset_id == asset_id,
                    LedgerTransaction.status == "posted",
                )
            )
            or 0
        )
        reservations = (
            await session.scalars(
                select(GatewayOperation).where(
                    GatewayOperation.merchant_id == merchant_id,
                    GatewayOperation.rail_id == connection.rail_id,
                    GatewayOperation.ledger_asset_id == asset_id,
                    GatewayOperation.operation_type == "payout",
                    GatewayOperation.status.in_(
                        ["submitting", "pending", "unknown", "reconciliation_required", "completed"]
                    ),
                )
            )
        ).all()
        held = sum(int(row.amount_atomic) for row in reservations if row.status != "completed")
        # Provider `available` already excludes acknowledged holds. Submissions
        # without trustworthy acknowledgement remain conservatively reserved.
        # Also account for every movement that could post after the read began.
        provider_reserve = sum(
            int(row.amount_atomic)
            for row in reservations
            if row.status in {"submitting", "unknown", "reconciliation_required"}
            or row.updated_at >= sampled_at
        )
        if balance < held:
            raise GatewayError("Ledger reservations require reconciliation")
        return balance, held, provider_reserve

    async def _open_invoice_atomic(self, session, rail_id, asset_id) -> int:
        # A paid but not yet ingested invoice may already sit in the provider
        # balance; never recognize it as pre-gateway liquidity.
        return int(
            await session.scalar(
                select(func.coalesce(func.sum(GatewayOperation.amount_atomic), 0)).where(
                    GatewayOperation.rail_id == rail_id,
                    GatewayOperation.ledger_asset_id == asset_id,
                    GatewayOperation.operation_type == "invoice",
                    GatewayOperation.status.in_(
                        ["submitting", "pending", "unknown", "reconciliation_required"]
                    ),
                )
            )
            or 0
        )

    async def _provider_balance(self, snapshot):
        adapter = self._build(snapshot)
        sampled_at = datetime.now(UTC)
        try:
            actual = account_digest(
                snapshot.rail_type, snapshot.network, await adapter.account_identity()
            )
            if not hmac.compare_digest(actual, snapshot.account_identity):
                raise GatewayError("Provider account identity changed")
            if not adapter.capabilities.balances:
                return None, sampled_at, adapter.capabilities.payout_mode
            return (
                {row.asset: row for row in await adapter.get_balances()},
                sampled_at,
                adapter.capabilities.payout_mode,
            )
        finally:
            await adapter.close()

    async def balances(self, merchant_id: UUID, rail_id: UUID) -> list[dict]:
        self.require_enabled(merchant_id)
        async with self.factory() as session:
            snapshot, _ = await self._connection(session, merchant_id, rail_id)
            symbols = list((await session.get(RailModel, rail_id)).assets)
        try:
            provider, sampled_at, mode = await self._provider_balance(snapshot)
            availability = "verified" if provider is not None else "manual"
        except RailError:
            provider, sampled_at, mode, availability = None, datetime.now(UTC), "unknown", "unknown"
        result = []
        async with self.factory() as session:
            _, connection = await self._connection(session, merchant_id, rail_id)
            for symbol in symbols:
                decimals = asset_decimals(snapshot.rail_type, symbol)
                asset = await session.scalar(
                    select(LedgerAsset).where(
                        LedgerAsset.network_kind == "custodial",
                        LedgerAsset.network_identifier
                        == f"{snapshot.rail_type}:{snapshot.network}",
                        LedgerAsset.canonical_identifier == symbol,
                        LedgerAsset.is_native.is_(False),
                    )
                )
                ledger, held, provider_reserve = (
                    await self._funds(session, merchant_id, connection, asset.id, sampled_at)
                    if asset
                    else (0, 0, 0)
                )
                open_invoices = (
                    await self._open_invoice_atomic(session, rail_id, asset.id) if asset else 0
                )
                row = provider.get(symbol) if provider is not None else None
                available = unledgered = None
                state = availability
                if provider is not None:
                    if row is None:
                        state = "unknown"
                    else:
                        provider_atomic = decimal_to_atomic(
                            row.available, decimals, allow_zero=True
                        )
                        available = atomic_to_decimal(
                            max(0, min(ledger - held, provider_atomic - provider_reserve)), decimals
                        )
                        unledgered = atomic_to_decimal(
                            max(
                                0,
                                provider_atomic
                                - provider_reserve
                                - (ledger - held)
                                - open_invoices,
                            ),
                            decimals,
                        )
                result.append(
                    {
                        "rail_id": str(rail_id),
                        "asset": symbol,
                        "network": snapshot.network,
                        "ledger_balance": atomic_to_decimal(ledger, decimals),
                        "reserved": atomic_to_decimal(held, decimals),
                        "available": available,
                        "unledgered_available": unledgered,
                        "provider_available": row.available if row else None,
                        "provider_held": row.held if row else None,
                        "availability": state,
                        "payout_mode": mode,
                        "as_of": sampled_at.isoformat(),
                    }
                )
        return result

    async def create_payout(
        self,
        merchant_id: UUID,
        rail_id: UUID,
        amount: Decimal,
        asset: str,
        destination: str,
        idempotency_key: str,
        *,
        destination_type="telegram_user_id",
        recognize_unledgered=False,
    ) -> OperationResult:
        self.require_enabled(merchant_id)
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or not destination.isascii()
            or not destination.isdecimal()
            or int(destination) <= 0
            or destination_type not in {"telegram_user_id", "user_id"}
        ):
            raise GatewayError("Valid recipient and idempotency key are required", status_code=422)
        destination = str(int(destination))
        symbol = asset.upper()
        async with self.factory() as session:
            snapshot, _ = await self._connection(session, merchant_id, rail_id)
            if symbol not in (await session.get(RailModel, rail_id)).assets:
                raise GatewayError("Asset is unavailable for this method", status_code=422)
        try:
            decimals = asset_decimals(snapshot.rail_type, symbol)
            requested = decimal_to_atomic(amount, decimals)
        except ValueError as exc:
            raise GatewayError("Invalid exact payout amount", status_code=422) from exc
        request_identity = {
            "rail": str(rail_id),
            "asset": symbol,
            "amount": str(requested),
            "destination": destination,
            "destination_type": destination_type,
        }
        if recognize_unledgered:
            request_identity["recognize_unledgered"] = True
        request_digest = digest(request_identity)
        async with self.factory() as session:
            previous = await session.scalar(
                select(GatewayOperation).where(
                    GatewayOperation.merchant_id == merchant_id,
                    GatewayOperation.operation_type == "payout",
                    GatewayOperation.idempotency_key == idempotency_key,
                )
            )
            if previous is not None:
                if previous.request_digest != request_digest:
                    raise GatewayError("Idempotency key is bound to another payout request")
                return OperationResult.from_row(previous)
        provider, sampled_at, mode = await self._provider_balance(snapshot)
        if provider is None or mode not in {"check", "transfer"}:
            raise GatewayError(
                "This provider requires manual withdrawal", code="MANUAL_PAYOUT", status_code=422
            )
        if snapshot.rail_type == "cryptobot" and destination_type != "telegram_user_id":
            raise GatewayError("CryptoBot requires Telegram recipient", status_code=422)
        if symbol not in provider:
            raise GatewayError("Authoritative asset balance is unavailable", status_code=503)
        actual_available = decimal_to_atomic(provider[symbol].available, decimals, allow_zero=True)
        async with self.factory() as session, session.begin():
            snapshot, connection = await self._connection(session, merchant_id, rail_id)
            await session.scalar(
                select(LedgerAccount.id)
                .where(LedgerAccount.id == connection.payable_account_id)
                .with_for_update()
            )
            # The account lock serializes both reservations and journal debits.
            previous = await session.scalar(
                select(GatewayOperation).where(
                    GatewayOperation.merchant_id == merchant_id,
                    GatewayOperation.operation_type == "payout",
                    GatewayOperation.idempotency_key == idempotency_key,
                )
            )
            if previous is not None:
                if previous.request_digest != request_digest:
                    raise GatewayError("Idempotency key is bound to another payout request")
                return OperationResult.from_row(previous)
            asset_row = await self._asset(session, snapshot.rail_type, snapshot.network, symbol)
            ledger, held, provider_reserve = await self._funds(
                session, merchant_id, connection, asset_row.id, sampled_at
            )
            shortfall = requested - (ledger - held)
            open_invoices = 0
            if shortfall > 0 and recognize_unledgered:
                # The account belongs to this merchant alone (unique identity), so
                # funds no ledger fact explains predate the gateway and are its own.
                open_invoices = await self._open_invoice_atomic(session, rail_id, asset_row.id)
                surplus = actual_available - provider_reserve - (ledger - held) - open_invoices
                if shortfall <= surplus:
                    ledger += shortfall
            if requested > min(ledger - held, actual_available - provider_reserve):
                raise GatewayError(
                    "Insufficient verified available balance",
                    code="INSUFFICIENT_FUNDS",
                    status_code=422,
                )
            operation = GatewayOperation(
                id=uuid4(),
                merchant_id=merchant_id,
                rail_id=rail_id,
                operation_type="payout",
                invoice_id=None,
                ledger_asset_id=asset_row.id,
                amount_atomic=requested,
                destination=destination,
                destination_type=destination_type,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                status="submitting",
            )
            session.add(operation)
            await session.flush()
            operation_id = operation.id
            if shortfall > 0 and recognize_unledgered:
                await self._recognize(
                    session,
                    connection,
                    operation,
                    shortfall,
                    actual_available=actual_available,
                    provider_reserve=provider_reserve,
                    outstanding=ledger - shortfall - held,
                    open_invoices=open_invoices,
                    sampled_at=sampled_at,
                )
        adapter = self._build(snapshot)
        try:
            payout = await adapter.payout(
                RailPayoutRequest(
                    amount,
                    symbol,
                    destination,
                    operation_id=str(operation_id),
                    destination_type=destination_type,
                )
            )
        except RailError as exc:
            return await self._creation_failed(operation_id, merchant_id, exc)
        finally:
            await adapter.close()
        return await self._finish_payout(merchant_id, operation_id, payout)

    async def _recognize(
        self,
        session,
        connection,
        operation,
        amount,
        *,
        actual_available,
        provider_reserve,
        outstanding,
        open_invoices,
        sampled_at,
    ):
        recognition_id = uuid4()
        evidence = digest(
            {
                "recognition": str(recognition_id),
                "account": connection.account_identity,
                "rail": str(connection.rail_id),
                "asset": str(operation.ledger_asset_id),
                "amount": str(amount),
                "provider_available": str(actual_available),
                "provider_reserve": str(provider_reserve),
                "ledger_outstanding": str(outstanding),
                "open_invoices": str(open_invoices),
                "payout": str(operation.id),
                "sampled_at": sampled_at.isoformat(),
            }
        )
        journal = await LedgerPostingService(session).post_in_transaction(
            merchant_id=operation.merchant_id,
            source_namespace="gateway_liquidity_recognition",
            source_external_id=str(recognition_id),
            source_digest=evidence,
            idempotency_key=f"gateway-liquidity:{recognition_id}",
            lines=[
                LedgerLine(
                    connection.receivable_account_id,
                    operation.ledger_asset_id,
                    LedgerDirection.DEBIT,
                    amount,
                ),
                LedgerLine(
                    connection.payable_account_id,
                    operation.ledger_asset_id,
                    LedgerDirection.CREDIT,
                    amount,
                ),
            ],
        )
        session.add(
            GatewayLiquidityRecognition(
                id=recognition_id,
                merchant_id=operation.merchant_id,
                rail_id=connection.rail_id,
                ledger_asset_id=operation.ledger_asset_id,
                amount_atomic=amount,
                provider_available_atomic=actual_available,
                provider_reserve_atomic=provider_reserve,
                ledger_outstanding_atomic=outstanding,
                open_invoice_atomic=open_invoices,
                payout_operation_id=operation.id,
                evidence_digest=evidence,
                ledger_transaction_id=journal.id,
                sampled_at=sampled_at,
            )
        )
        await session.flush()

    async def _finish_payout(self, merchant_id, operation_id, payout: RailPayout):
        async with self.factory() as session, session.begin():
            row = await session.get(GatewayOperation, operation_id)
            if row is None or row.merchant_id != merchant_id or row.operation_type != "payout":
                raise GatewayError("Payout not found", status_code=404)
            _, connection = await self._connection(session, merchant_id, row.rail_id)
            await session.scalar(
                select(LedgerAccount.id)
                .where(LedgerAccount.id == connection.payable_account_id)
                .with_for_update()
            )
            row = await session.scalar(
                select(GatewayOperation)
                .where(GatewayOperation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row.status in {"completed", "failed", "reconciliation_required"}:
                return OperationResult.from_row(row)
            asset = await session.get(LedgerAsset, row.ledger_asset_id)
            try:
                paid_amount = decimal_to_atomic(payout.amount, asset.atomic_decimals)
            except (ValueError, TypeError, AttributeError):
                paid_amount = None
            if (
                paid_amount != int(row.amount_atomic)
                or payout.asset != asset.symbol
                or payout.destination != row.destination
                or payout.destination_type != row.destination_type
                or not payout.provider_payout_id
                or payout.status not in {"completed", "pending", "failed"}
                or (row.provider_reference and row.provider_reference != payout.provider_payout_id)
                or (payout.status == "completed" and not payout.success)
            ):
                row.status, row.failure_code = "reconciliation_required", "payout_binding_conflict"
                await session.flush()
                return OperationResult.from_row(row)
            row.provider_reference = payout.provider_payout_id
            row.pay_url = payout.check_url or None
            if payout.status == "completed":
                # Debit and receipt commit together. Until then, the durable
                # operation still reserves funds even after a process crash.
                await session.flush()
                journal = await LedgerPostingService(session).post_in_transaction(
                    merchant_id=merchant_id,
                    source_namespace="gateway_provider_payout",
                    source_external_id=str(row.id),
                    source_digest=row.request_digest,
                    idempotency_key=f"gateway-payout:{row.id}",
                    lines=[
                        LedgerLine(
                            connection.payable_account_id,
                            asset.id,
                            LedgerDirection.DEBIT,
                            int(row.amount_atomic),
                        ),
                        LedgerLine(
                            connection.receivable_account_id,
                            asset.id,
                            LedgerDirection.CREDIT,
                            int(row.amount_atomic),
                        ),
                    ],
                )
                session.add(
                    GatewayPayoutReceipt(
                        operation_id=row.id,
                        merchant_id=merchant_id,
                        provider_account_identity=connection.account_identity,
                        provider_reference=payout.provider_payout_id,
                        evidence_digest=digest(
                            {
                                "request": row.request_digest,
                                "reference": payout.provider_payout_id,
                                "amount": str(paid_amount),
                                "asset": payout.asset,
                                "status": payout.status,
                                "destination": payout.destination,
                                "destination_type": payout.destination_type,
                            }
                        ),
                        ledger_transaction_id=journal.id,
                    )
                )
                await session.flush()
            row.status, row.failure_code = payout.status, None
            await session.flush()
            return OperationResult.from_row(row)

    async def reconcile_payout(self, merchant_id: UUID, operation_id: UUID) -> OperationResult:
        self.require_enabled(merchant_id)
        async with self.factory() as session, session.begin():
            operation = await session.scalar(
                select(GatewayOperation)
                .where(GatewayOperation.id == operation_id)
                .with_for_update()
            )
            if (
                operation is None
                or operation.merchant_id != merchant_id
                or operation.operation_type != "payout"
            ):
                raise GatewayError("Payout not found", status_code=404)
            if operation.status in {"completed", "failed", "reconciliation_required"}:
                return OperationResult.from_row(operation)
            operation.updated_at = datetime.now(UTC)
            await session.flush()
            snapshot, _ = await self._connection(session, merchant_id, operation.rail_id)
            reference = operation.provider_reference or ""
        adapter = self._build(snapshot)
        try:
            if not adapter.capabilities.payout_lookup_by_client_id:
                return await self.get_operation(merchant_id, operation_id)
            actual = account_digest(
                snapshot.rail_type, snapshot.network, await adapter.account_identity()
            )
            if not hmac.compare_digest(actual, snapshot.account_identity):
                raise GatewayError("Provider account identity changed")
            payout = await adapter.get_payout(str(operation_id), reference)
            return await self._finish_payout(merchant_id, operation_id, payout)
        except RailError:
            return await self.get_operation(merchant_id, operation_id)
        finally:
            await adapter.close()
