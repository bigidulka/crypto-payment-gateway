"""
Webhook Dispatcher Worker.
Отправляет webhooks из outbox таблицы.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp
from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from src.core.config import get_settings
from src.core.security import generate_hmac_signature
from src.core.webhook_egress import (
    WebhookEgressError,
    WebhookResolutionError,
    pinned_webhook_session,
    resolve_webhook_destination,
)
from src.db.models import OutboxStatus, OutboxWebhook, Webhook
from src.db.session import get_session_context

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 4096


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Non-sensitive delivery outcome for the outbox state machine."""

    success: bool
    error_code: str = ""


async def close_http_client() -> None:
    """Compatibility hook: delivery sessions are per-request and already closed."""
    return None


async def send_webhook(
    webhook: Webhook,
    outbox: OutboxWebhook,
    timeout: int = 30,
) -> WebhookDeliveryResult:
    """Deliver one signed webhook through a DNS-pinned no-redirect session."""
    payload_bytes = json.dumps(outbox.payload).encode()
    signature, timestamp = generate_hmac_signature(payload_bytes, webhook.secret)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Event": outbox.event_type,
    }

    try:
        # One deadline covers DNS, TLS/connect, request write and bounded read.
        # DNS itself receives the same remaining budget; it cannot consume an
        # additional full timeout before the HTTP client starts.
        async with asyncio.timeout(timeout):
            destination = await resolve_webhook_destination(
                webhook.url,
                timeout_seconds=timeout,
            )
            async with pinned_webhook_session(destination, timeout_seconds=timeout) as client:
                async with client.post(
                    destination.url,
                    data=payload_bytes,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    await response.content.read(_MAX_RESPONSE_BYTES)
                    if 200 <= response.status < 300:
                        logger.info("Webhook %s delivered", outbox.id)
                        return WebhookDeliveryResult(success=True)
                    logger.warning(
                        "Webhook %s delivery returned HTTP %s",
                        outbox.id,
                        response.status,
                    )
                    return WebhookDeliveryResult(
                        success=False,
                        error_code=f"http_{response.status}",
                    )
    except WebhookResolutionError:
        logger.warning("Webhook %s DNS resolution unavailable", outbox.id)
        return WebhookDeliveryResult(success=False, error_code="dns_resolution_failed")
    except WebhookEgressError:
        logger.warning("Webhook %s blocked by egress policy", outbox.id)
        return WebhookDeliveryResult(success=False, error_code="egress_policy_blocked")
    except TimeoutError:
        logger.warning("Webhook %s timed out", outbox.id)
        return WebhookDeliveryResult(success=False, error_code="timeout")
    except aiohttp.ClientError:
        logger.warning("Webhook %s transport failed", outbox.id)
        return WebhookDeliveryResult(success=False, error_code="transport_error")


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
                    OutboxWebhook.next_retry_at <= datetime.now(UTC),
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

            delivery = await send_webhook(
                webhook,
                outbox,
                timeout=settings.webhook_timeout_seconds,
            )

            outbox.attempt += 1

            if delivery.success:
                outbox.status = OutboxStatus.SENT
                outbox.sent_at = datetime.now(UTC)
            else:
                # Exponential backoff: 1, 2, 4, 8, 16 minutes. Existing
                # legacy/internal records are retained, never replayed by this
                # patch; a policy block receives a non-sensitive reason.
                backoff_minutes = 2 ** (outbox.attempt - 1)
                outbox.next_retry_at = datetime.now(UTC) + timedelta(
                    minutes=backoff_minutes
                )
                outbox.last_error = delivery.error_code or "delivery_failed"

                if outbox.attempt >= outbox.max_attempts:
                    outbox.status = OutboxStatus.FAILED
                    logger.warning(
                        "Webhook %s failed permanently after %s attempts",
                        outbox.id,
                        outbox.attempt,
                    )
            processed += 1
        await session.commit()

    return processed


async def run_dispatcher() -> None:
    """Run the webhook dispatcher until the worker is stopped."""
    logger.info("Starting webhook dispatcher")

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
