"""
Сервис для работы с webhooks.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundError
from src.core.security import generate_webhook_secret
from src.db.models import Merchant, Webhook

logger = logging.getLogger(__name__)


class WebhookService:
    """Сервис для работы с webhooks."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_webhook(
        self,
        merchant: Merchant,
        url: str,
        events: list[str],
    ) -> Webhook:
        """
        Создать новый webhook.

        Args:
            merchant: Мерчант
            url: URL для отправки событий
            events: Список событий для отправки

        Returns:
            Созданный webhook
        """
        # Генерируем секрет для подписи
        secret = generate_webhook_secret()

        webhook = Webhook(
            id=uuid.uuid4(),
            merchant_id=merchant.id,
            url=url,
            secret=secret,
            events=events,
            is_active=True,
        )

        self.session.add(webhook)
        await self.session.commit()
        await self.session.refresh(webhook)

        logger.info(f"Created webhook {webhook.id} for merchant {merchant.id}")
        return webhook

    async def get_webhook(
        self,
        webhook_id: uuid.UUID,
        merchant_id: uuid.UUID,
    ) -> Webhook:
        """
        Получить webhook по ID.

        Raises:
            NotFoundError: Если webhook не найден
        """
        stmt = select(Webhook).where(
            Webhook.id == webhook_id,
            Webhook.merchant_id == merchant_id,
        )
        result = await self.session.execute(stmt)
        webhook = result.scalar_one_or_none()

        if webhook is None:
            raise NotFoundError("Webhook", str(webhook_id))

        return webhook

    async def list_webhooks(self, merchant_id: uuid.UUID) -> list[Webhook]:
        """Получить все webhooks мерчанта."""
        stmt = (
            select(Webhook)
            .where(Webhook.merchant_id == merchant_id)
            .order_by(Webhook.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def delete_webhook(
        self,
        webhook_id: uuid.UUID,
        merchant_id: uuid.UUID,
    ) -> None:
        """
        Удалить webhook.

        Raises:
            NotFoundError: Если webhook не найден
        """
        webhook = await self.get_webhook(webhook_id, merchant_id)
        await self.session.delete(webhook)
        await self.session.commit()

        logger.info(f"Deleted webhook {webhook_id}")

    async def update_webhook_status(
        self,
        webhook_id: uuid.UUID,
        merchant_id: uuid.UUID,
        is_active: bool,
    ) -> Webhook:
        """Обновить статус webhook."""
        webhook = await self.get_webhook(webhook_id, merchant_id)
        webhook.is_active = is_active
        await self.session.commit()
        await self.session.refresh(webhook)
        return webhook
