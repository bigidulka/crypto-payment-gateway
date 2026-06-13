"""
Invoice Expirer Worker.
Истекает invoices с истёкшим TTL.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from src.db.models import Invoice, InvoiceStatus, OutboxStatus, OutboxWebhook, Webhook
from src.db.session import get_session_context

logger = logging.getLogger(__name__)


def build_invoice_payload(invoice: Invoice) -> dict:
    """Собрать payload для webhook события."""
    return {
        "invoice_id": str(invoice.public_id),
        "merchant_order_id": invoice.merchant_order_id or "",
        "status": invoice.status.value,
        "amount": str(invoice.amount),
        "asset": invoice.asset,
        "expired_at": invoice.expires_at.isoformat() if invoice.expires_at else None,
    }


async def expire_overdue_invoices() -> int:
    """
    Найти и пометить expired invoices.

    Returns:
        Количество истёкших invoices
    """
    expired_count = 0

    async with get_session_context() as session:
        # Находим invoices с истёкшим expires_at
        stmt = (
            select(Invoice)
            .options(selectinload(Invoice.merchant))
            .where(
                and_(
                    Invoice.status.in_(
                        [
                            InvoiceStatus.CREATED,
                            InvoiceStatus.AWAITING_PAYMENT,
                        ]
                    ),
                    Invoice.expires_at <= datetime.now(timezone.utc),
                )
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        invoices = result.scalars().all()

        for invoice in invoices:
            logger.info(f"Expiring invoice {invoice.public_id}")

            invoice.status = InvoiceStatus.EXPIRED

            # Найти активный webhook для мерчанта с подпиской на invoice.expired
            webhook_stmt = select(Webhook).where(
                and_(
                    Webhook.merchant_id == invoice.merchant_id,
                    Webhook.is_active == True,
                )
            )
            webhook_result = await session.execute(webhook_stmt)
            webhooks = webhook_result.scalars().all()

            # Создать outbox для каждого подходящего webhook
            for webhook in webhooks:
                # Проверяем, подписан ли webhook на invoice.expired
                if "invoice.expired" in webhook.events or "*" in webhook.events:
                    outbox = OutboxWebhook(
                        id=uuid.uuid4(),
                        webhook_id=webhook.id,
                        invoice_id=invoice.id,
                        event_type="invoice.expired",
                        payload=build_invoice_payload(invoice),
                        status=OutboxStatus.PENDING,
                    )
                    session.add(outbox)
                    logger.info(
                        f"Created webhook outbox for expired invoice {invoice.public_id}"
                    )

            expired_count += 1

        await session.commit()

    return expired_count


async def run_expirer() -> None:
    """
    Главный цикл Invoice Expirer worker.
    """
    logger.info("Starting Invoice Expirer worker")

    while True:
        try:
            expired = await expire_overdue_invoices()
            if expired > 0:
                logger.info(f"Expired {expired} invoices")
        except Exception as e:
            logger.error(f"Error expiring invoices: {e}")

        # Пауза между итерациями (30 секунд)
        await asyncio.sleep(30)


# ARQ Worker Settings
class WorkerSettings:
    """Настройки для ARQ worker."""

    @staticmethod
    async def run_worker():
        """Запуск worker."""
        await run_expirer()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_expirer())
