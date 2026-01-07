"""
Сервис для работы с инвойсами.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_chain_config
from src.core.config import get_settings
from src.core.exceptions import (
    DuplicateError,
    InvoiceExpiredError,
    InvoiceNotFoundError,
    ValidationError,
)
from src.core.security import generate_public_id
from src.db.models import (
    Invoice,
    InvoiceEvent,
    InvoiceStatus,
    Merchant,
    OnchainTx,
    OutboxStatus,
    OutboxWebhook,
    PaymentSession,
    Webhook,
)

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
        # Проверяем idempotency key
        if idempotency_key:
            existing = await self._find_by_idempotency_key(merchant.id, idempotency_key)
            if existing:
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
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

        self.session.add(invoice)

        # Создаём событие
        event = InvoiceEvent(
            invoice_id=invoice.id,
            event_type="invoice.created",
            payload={
                "amount": str(amount),
                "asset": asset,
                "allowed_chains": allowed_chains,
            },
        )
        self.session.add(event)

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

        # Событие
        event = InvoiceEvent(
            invoice_id=invoice.id,
            event_type="invoice.expired",
            payload={"reason": "manual"},
        )
        self.session.add(event)

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
        event = InvoiceEvent(
            invoice_id=invoice.id,
            event_type=event_type,
            payload=event_payload or {"old_status": old_status.value},
        )
        self.session.add(event)

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
