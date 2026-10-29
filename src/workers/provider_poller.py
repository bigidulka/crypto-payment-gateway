"""Read-only provider reconciliation; never retries invoice/payout submissions."""

import asyncio
import logging

from src.core.config import get_settings
from src.db.session import (
    close_db,
    get_session_factory,
    require_runtime_database_ready,
    verify_limited_runtime_database_role,
)
from src.services.gateway_provider_service import GatewayProviderService

logger = logging.getLogger(__name__)


async def run():
    settings = get_settings()
    if not settings.ledger_rail_orchestration_enabled:
        logger.info("Provider payments are disabled")
        return
    await require_runtime_database_ready()
    await verify_limited_runtime_database_role()
    service = GatewayProviderService(get_session_factory())
    try:
        while True:
            try:
                await service.poll_batch(settings.gateway_provider_poll_batch_size)
            except Exception:
                # No exception body: provider messages can contain credentials.
                logger.error("Provider reconciliation pass failed; reservations remain intact")
            await asyncio.sleep(settings.gateway_provider_poll_interval_seconds)
    finally:
        await close_db()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
