"""
Сервис для работы с инвойсами.
"""

import logging
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import get_settings
from src.blockchain.chains import get_chain_config
from src.core.exceptions import (
    InvoiceNotFoundError,
    ValidationError,
)
from src.core.security import generate_public_id
from src.db.models import (
    Invoice,
    GatewayInvoiceOptions,
    GatewayRailConnection,
    InvoiceEvent,
    InvoiceStatus,
    Merchant,
    OutboxStatus,
    OutboxWebhook,
    PaymentSession,
    PaymentSessionStatus,
    Rail,
    Webhook,
)
from src.services.address_lease_service import AddressLeaseService
from src.ledger.amounts import decimal_to_atomic
from src.payments.policy import asset_decimals

logger = logging.getLogger(__name__)


class InvoiceService:
    """Сервис для работы с инвойсами."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    async def create_invoice(
        self,
        merchant: Merchant,
        amount: Decimal,
        asset: str,
        allowed_chains: list[str],
        ttl_minutes: int = 60,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allowed_rails: list[uuid.UUID] | None = None,
        customer_telegram_id: str = "",
    ) -> Invoice:
        """
        Создать новый инвойс.

        Args:
            merchant: Мерчант-владелец
            amount: Сумма платежа
            asset: Актив (USDT/USDC)
            allowed_chains: Разрешённые сети
            ttl_minutes: Время жизни в минутах
            metadata: Произвольные метаданные
            idempotency_key: Ключ идемпотентности

        Returns:
            Созданный инвойс

        Raises:
            DuplicateError: Если idempotency_key уже использован
        """
        allowed_rails = allowed_rails or []
        if not amount.is_finite() or amount <= 0 or not (allowed_chains or allowed_rails):
            raise ValidationError("Positive amount and at least one payment method are required")
        try:
            if decimal_to_atomic(amount, 18) >= 10**36:
                raise ValueError("invoice amount exceeds storage bounds")
            for chain in allowed_chains:
                decimal_to_atomic(amount, get_chain_config(chain).get_asset_decimals(asset))
        except ValueError as exc:
            raise ValidationError("Amount exceeds invoice bounds or payment method precision") from exc
        if len(set(allowed_rails)) != len(allowed_rails):
            raise ValidationError("Duplicate provider payment methods")
        if customer_telegram_id and (not customer_telegram_id.isascii()
                or not customer_telegram_id.isdecimal() or int(customer_telegram_id) <= 0):
            raise ValidationError("Customer Telegram ID must be a positive numeric ID")
        if allowed_rails:
            if (not self.settings.ledger_rail_orchestration_enabled
                    or str(merchant.id) not in self.settings.ledger_rail_orchestration_merchant_ids):
                raise ValidationError("Provider payments are not enabled for this merchant")
            for rail_id in allowed_rails:
                row = await self.session.scalar(select(Rail).join(
                    GatewayRailConnection, GatewayRailConnection.rail_id == Rail.id).where(
                    Rail.id == rail_id, Rail.merchant_id == merchant.id, Rail.is_active.is_(True)))
                if row is None or asset.upper() not in row.assets:
                    raise ValidationError("Provider method or asset is unavailable")
                if row.rail_type == "tgwallet" and not customer_telegram_id:
                    raise ValidationError("Wallet Pay requires a trusted Telegram customer ID")
                try:
                    decimal_to_atomic(amount, asset_decimals(row.rail_type, asset.upper()))
                except ValueError as exc:
                    raise ValidationError("Amount exceeds the payment method precision") from exc
        text_amount = format(amount, "f")
        if "." in text_amount:
            text_amount = text_amount.rstrip("0").rstrip(".")
        request_digest = hashlib.sha256(json.dumps({
            "amount": text_amount, "asset": asset.upper(), "chains": sorted(allowed_chains),
            "rails": sorted(str(value) for value in allowed_rails), "ttl": ttl_minutes,
            "metadata": metadata, "customer": customer_telegram_id,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        # Serialize merchant-key allocation; a concurrent replay sees the
        # committed request instead of failing a uniqueness constraint.
        await self.session.scalar(select(Merchant.id).where(Merchant.id == merchant.id).with_for_update())
        # Проверяем idempotency key
        if idempotency_key:
            existing = await self._find_by_idempotency_key(merchant.id, idempotency_key)
            if existing:
                options = None
                if self.settings.ledger_rail_orchestration_enabled:
                    options = await self.session.get(GatewayInvoiceOptions, existing.id)
                if options is not None:
                    same = options.request_digest == request_digest
                else:
                    same = (not allowed_rails and not customer_telegram_id
                        and existing.amount == amount and existing.asset == asset.upper()
                        and sorted(existing.allowed_chains) == sorted(allowed_chains)
                        and existing.ttl_minutes == ttl_minutes and existing.extra_data == metadata)
                if not same:
                    raise ValidationError("Idempotency key is already bound to a different invoice request")
                logger.info(
                    f"Returning existing invoice {existing.id} for idempotency key"
                )
                return existing

        # Создаём инвойс
        now = datetime.now(timezone.utc)
        invoice = Invoice(
            id=uuid.uuid4(),
            public_id=generate_public_id("PAY"),
            merchant_id=merchant.id,
            amount=amount,
            asset=asset.upper(),
            allowed_chains=allowed_chains,
            status=InvoiceStatus.CREATED,
            ttl_minutes=ttl_minutes,
            expires_at=now + timedelta(minutes=ttl_minutes),
            extra_data=metadata,
            idempotency_key=idempotency_key,
        )

        self.session.add(invoice)
        if allowed_rails:
            await self.session.flush()
            self.session.add(GatewayInvoiceOptions(invoice_id=invoice.id, merchant_id=merchant.id,
                allowed_rails=[str(value) for value in allowed_rails], customer_telegram_id=customer_telegram_id,
                request_digest=request_digest))

        # Создаём событие
        await self._record_legacy_event(invoice.id, "invoice.created", {
                "amount": str(amount),
                "asset": asset,
                "allowed_chains": allowed_chains,
            })

        # Создаём webhook outbox записи
        await self._create_webhook_outbox(merchant.id, invoice, "invoice.created")

        await self.session.commit()
        await self.session.refresh(invoice)

        logger.info(f"Created invoice {invoice.id} for merchant {merchant.id}")
        return invoice

    async def get_invoice(
        self, invoice_id: uuid.UUID, merchant_id: uuid.UUID
    ) -> Invoice:
        """
        Получить инвойс по ID.

        Args:
            invoice_id: ID инвойса
            merchant_id: ID мерчанта (для проверки доступа)

        Returns:
            Инвойс

        Raises:
            InvoiceNotFoundError: Если инвойс не найден
        """
        stmt = (
            select(Invoice)
            .options(
                selectinload(Invoice.payment_sessions).selectinload(
                    PaymentSession.onchain_txs
                )
            )
            .where(Invoice.id == invoice_id)
            .where(Invoice.merchant_id == merchant_id)
        )
        result = await self.session.execute(stmt)
        invoice = result.scalar_one_or_none()

        if invoice is None:
            raise InvoiceNotFoundError(str(invoice_id))

        return invoice

    async def provider_rail_ids(self, invoice: Invoice) -> list[uuid.UUID]:
        if not self.settings.ledger_rail_orchestration_enabled:
            return []
        row = await self.session.get(GatewayInvoiceOptions, invoice.id)
        return [uuid.UUID(value) for value in row.allowed_rails] if row else []

    async def get_invoice_by_public_id(self, public_id: str) -> Invoice:
        """
        Получить инвойс по публичному ID.

        Args:
            public_id: Публичный ID инвойса

        Returns:
            Инвойс

        Raises:
            InvoiceNotFoundError: Если инвойс не найден
        """
        stmt = (
            select(Invoice)
            .options(
                selectinload(Invoice.merchant),
                selectinload(Invoice.payment_sessions).selectinload(
                    PaymentSession.onchain_txs
                ),
                selectinload(Invoice.payment_sessions).selectinload(
                    PaymentSession.deposit_address
                ),
            )
            .where(Invoice.public_id == public_id)
        )
        result = await self.session.execute(stmt)
        invoice = result.scalar_one_or_none()

        if invoice is None:
            raise InvoiceNotFoundError(public_id)

        return invoice

    async def list_invoices(
        self,
        merchant_id: uuid.UUID,
        status: InvoiceStatus | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Invoice], int]:
        """
        Получить список инвойсов мерчанта.

        Returns:
            Tuple[список инвойсов, общее количество]
        """
        # Базовый запрос
        base_query = select(Invoice).where(Invoice.merchant_id == merchant_id)

        # Фильтры
        if status:
            base_query = base_query.where(Invoice.status == status)
        if from_date:
            base_query = base_query.where(Invoice.created_at >= from_date)
        if to_date:
            base_query = base_query.where(Invoice.created_at <= to_date)

        # Подсчёт общего количества
        count_query = select(func.count()).select_from(base_query.subquery())
        total = await self.session.scalar(count_query) or 0

        # Получаем записи с пагинацией
        stmt = (
            base_query.options(selectinload(Invoice.payment_sessions))
            .order_by(Invoice.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        invoices = list(result.scalars().all())

        return invoices, total

    async def expire_invoice(
        self, invoice_id: uuid.UUID, merchant_id: uuid.UUID
    ) -> Invoice:
        """
        Принудительно завершить инвойс (пометить как expired).

        Args:
            invoice_id: ID инвойса
            merchant_id: ID мерчанта

        Returns:
            Обновлённый инвойс
        """
        invoice = await self.get_invoice(invoice_id, merchant_id)

        if invoice.status not in (
            InvoiceStatus.CREATED,
            InvoiceStatus.AWAITING_PAYMENT,
        ):
            raise ValidationError(
                f"Cannot expire invoice in status {invoice.status}",
                details={"current_status": invoice.status},
            )

        invoice.status = InvoiceStatus.EXPIRED

        lease_service = AddressLeaseService(self.session)
        for payment_session in invoice.payment_sessions:
            if payment_session.status in (
                PaymentSessionStatus.PENDING,
                PaymentSessionStatus.SEEN_ONCHAIN,
            ):
                await lease_service.release_to_cooldown(
                    payment_session,
                    lease_service.cooldown_until_for_invoice(invoice),
                    status=PaymentSessionStatus.EXPIRED,
                    reason="invoice_expired_manual",
                )

        # Событие
        await self._record_legacy_event(invoice.id, "invoice.expired", {"reason": "manual"})

        # Webhook
        await self._create_webhook_outbox(merchant_id, invoice, "invoice.expired")

        await self.session.commit()
        await self.session.refresh(invoice)

        logger.info(f"Manually expired invoice {invoice.id}")
        return invoice

    async def update_invoice_status(
        self,
        invoice: Invoice,
        new_status: InvoiceStatus,
        event_payload: dict[str, Any] | None = None,
    ) -> None:
        """
        Обновить статус инвойса.

        Args:
            invoice: Инвойс
            new_status: Новый статус
            event_payload: Данные для события
        """
        old_status = invoice.status
        invoice.status = new_status

        # Событие
        event_type = f"invoice.{new_status.value.lower()}"
        await self._record_legacy_event(invoice.id, event_type, event_payload or {"old_status": old_status.value})

        # Webhook (если есть соответствующее событие)
        webhook_events = {
            InvoiceStatus.SEEN_ONCHAIN: "invoice.seen_onchain",
            InvoiceStatus.CONFIRMED: "invoice.confirmed",
            InvoiceStatus.EXPIRED: "invoice.expired",
        }

        if new_status in webhook_events:
            await self._create_webhook_outbox(
                invoice.merchant_id,
                invoice,
                webhook_events[new_status],
            )

        await self.session.commit()
        logger.info(
            f"Invoice {invoice.id} status changed: {old_status} -> {new_status}"
        )

    async def _find_by_idempotency_key(
        self,
        merchant_id: uuid.UUID,
        idempotency_key: str,
    ) -> Invoice | None:
        """Найти инвойс по idempotency key."""
        stmt = select(Invoice).where(
            and_(
                Invoice.merchant_id == merchant_id,
                Invoice.idempotency_key == idempotency_key,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _record_legacy_event(self, invoice_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> None:
        # 0010 lacks event_id. Explicit Core columns keep feature-off legacy
        # writes compatible without querying or modifying historical schema.
        await self.session.flush()
        await self.session.execute(insert(InvoiceEvent.__table__).values(
            id=uuid.uuid4(), invoice_id=invoice_id, event_type=event_type, payload=payload))

    async def _create_webhook_outbox(
        self,
        merchant_id: uuid.UUID,
        invoice: Invoice,
        event_type: str,
    ) -> None:
        """Создать записи в outbox для всех webhooks мерчанта."""
        # Получаем webhooks мерчанта
        stmt = select(Webhook).where(
            and_(
                Webhook.merchant_id == merchant_id,
                Webhook.is_active == True,  # noqa: E712
            )
        )
        result = await self.session.execute(stmt)
        webhooks = result.scalars().all()

        # Формируем payload
        payload = {
            "event": event_type,
            "invoice": {
                "id": str(invoice.id),
                "public_id": invoice.public_id,
                "amount": str(invoice.amount),
                "asset": invoice.asset,
                "status": invoice.status.value,
                "expires_at": invoice.expires_at.isoformat(),
                "metadata": invoice.extra_data,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Создаём outbox записи
        for webhook in webhooks:
            if event_type in webhook.events:
                outbox = OutboxWebhook(
                    webhook_id=webhook.id,
                    invoice_id=invoice.id,
                    event_type=event_type,
                    payload=payload,
                    status=OutboxStatus.PENDING,
                )
                self.session.add(outbox)

    def get_hosted_url(self, invoice: Invoice) -> str:
        """Получить URL hosted страницы для инвойса."""
        return f"{self.settings.hosted_base_url}/pay/{invoice.public_id}"
