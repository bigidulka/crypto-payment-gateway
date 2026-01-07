"""
Webhook Dispatcher Worker.
Отправляет webhooks из outbox таблицы.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from src.core.config import get_settings
from src.core.security import generate_hmac_signature
from src.db.models import OutboxStatus, OutboxWebhook, Webhook
from src.db.session import get_session_context

logger = logging.getLogger(__name__)

# Глобальный httpx client с connection pooling
_http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Получить httpx client с connection pooling."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _http_client


async def close_http_client() -> None:
    """Закрыть httpx client."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


async def send_webhook(
    webhook: Webhook,
    outbox: OutboxWebhook,
    timeout: int = 30,
) -> bool:
    """
    Отправить webhook.

    Returns:
        True если успешно отправлено
    """
    # Формируем payload
    import json

    payload_bytes = json.dumps(outbox.payload).encode()

    # Генерируем подпись
    signature, timestamp = generate_hmac_signature(payload_bytes, webhook.secret)

    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Event": outbox.event_type,
    }

    try:
        # Используем глобальный client с connection pooling
        client = await get_http_client()
        response = await client.post(
            webhook.url,
            content=payload_bytes,
            headers=headers,
            timeout=timeout,
        )

        # Считаем успехом любой 2xx ответ
        if 200 <= response.status_code < 300:
            logger.info(f"Webhook {outbox.id} sent successfully to {webhook.url}")
            return True
        else:
            logger.warning(
                f"Webhook {outbox.id} failed: status={response.status_code}, "
                f"body={response.text[:200]}"
            )
            return False

    except httpx.TimeoutException:
        logger.warning(f"Webhook {outbox.id} timed out")
        return False
    except Exception as e:
        logger.error(f"Webhook {outbox.id} error: {e}")
        return False


async def process_pending_webhooks() -> int:
    """
    Обработать pending webhooks.

    Returns:
        Количество обработанных webhooks
    """
    settings = get_settings()
    processed = 0

    async with get_session_context() as session:
        # Получаем pending webhooks, готовые к отправке
        stmt = (
            select(OutboxWebhook)
            .options(selectinload(OutboxWebhook.webhook))
            .where(
                and_(
                    OutboxWebhook.status == OutboxStatus.PENDING,
                    OutboxWebhook.next_retry_at <= datetime.now(timezone.utc),
                    OutboxWebhook.attempt < OutboxWebhook.max_attempts,
                )
            )
            .limit(50)  # Batch size
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        outbox_items = result.scalars().all()

        for outbox in outbox_items:
            webhook = outbox.webhook

            if not webhook or not webhook.is_active:
                # Webhook отключён или удалён
                outbox.status = OutboxStatus.FAILED
                outbox.last_error = "Webhook is inactive or deleted"
                continue

            # Отправляем webhook
            success = await send_webhook(
                webhook,
                outbox,
                timeout=settings.webhook_timeout_seconds,
            )

            outbox.attempt += 1

            if success:
                outbox.status = OutboxStatus.SENT
                outbox.sent_at = datetime.now(timezone.utc)
            else:
                # Экспоненциальный backoff: 1, 2, 4, 8, 16 минут
                backoff_minutes = 2 ** (outbox.attempt - 1)
                outbox.next_retry_at = datetime.now(timezone.utc) + timedelta(
                    minutes=backoff_minutes
                )
                outbox.last_error = "Delivery failed"

                # Если исчерпаны попытки
                if outbox.attempt >= outbox.max_attempts:
                    outbox.status = OutboxStatus.FAILED
                    logger.warning(
                        f"Webhook {outbox.id} failed permanently after {outbox.attempt} attempts"
                    )

            processed += 1

        await session.commit()

    return processed


async def run_dispatcher() -> None:
    """
    Главный цикл Webhook Dispatcher.
    """
    settings = get_settings()

    logger.info("Starting Webhook Dispatcher")

    try:
        while True:
            try:
                processed = await process_pending_webhooks()
                if processed > 0:
                    logger.info(f"Processed {processed} webhooks")
            except Exception as e:
                logger.error(f"Error processing webhooks: {e}")

            # Пауза между итерациями (5 секунд)
            await asyncio.sleep(5)
    finally:
        # Закрываем http client при shutdown
        await close_http_client()
        logger.info("HTTP client closed")


# ARQ Worker Settings
class WorkerSettings:
    """Настройки для ARQ worker."""

    @staticmethod
    async def run_worker():
        """Запуск worker."""
        await run_dispatcher()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_dispatcher())
