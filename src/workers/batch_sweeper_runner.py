"""
Batch Sweeper Runner - периодический вывод средств с persistent адресов.

Запускает batch_sweeper каждые N минут для автоматического
вывода средств с persistent deposit адресов на treasury.
"""

import asyncio
import logging
from datetime import datetime, timezone

from src.core.config import get_settings
from src.workers.batch_sweeper import run_batch_sweep

logger = logging.getLogger(__name__)

# Интервал между sweep циклами (в секундах)
# По умолчанию 5 минут
SWEEP_INTERVAL_SECONDS = 300


async def run_batch_sweeper_loop() -> None:
    """
    Главный цикл Batch Sweeper.
    
    Периодически запускает batch sweep для всех persistent адресов.
    """
    settings = get_settings()
    
    if not settings.treasury_address:
        logger.warning("TREASURY_ADDRESS not configured, batch sweeper disabled")
        while True:
            await asyncio.sleep(60)
    
    if not settings.funder_private_key.get_secret_value():
        logger.warning("FUNDER_PRIVATE_KEY not configured, batch sweeper disabled")
        while True:
            await asyncio.sleep(60)
    
    logger.info(
        f"Starting Batch Sweeper (interval: {SWEEP_INTERVAL_SECONDS}s, "
        f"treasury: {settings.treasury_address[:10]}...)"
    )
    
    iteration = 0
    while True:
        iteration += 1
        
        try:
            logger.info(f"[Batch Sweeper] Starting sweep cycle #{iteration}")
            start_time = datetime.now(timezone.utc)
            
            # Запускаем sweep только для persistent адресов
            results = await run_batch_sweep(
                chains=None,  # Все сети
                dry_run=False,
                include_deposits=False,  # Не трогаем invoice deposits (их обрабатывает sweeper)
                include_persistent=True,  # Только persistent адреса
            )
            
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            
            if results:
                total_swept = sum(r.swept_count for r in results.values())
                total_amount = sum(r.total_amount for r in results.values())
                total_failed = sum(r.failed_count for r in results.values())
                
                if total_swept > 0 or total_failed > 0:
                    logger.info(
                        f"[Batch Sweeper] Cycle #{iteration} complete: "
                        f"swept={total_swept}, amount=${total_amount:.2f}, "
                        f"failed={total_failed}, duration={duration:.1f}s"
                    )
                else:
                    logger.debug(
                        f"[Batch Sweeper] Cycle #{iteration}: no wallets to sweep"
                    )
            else:
                logger.debug(f"[Batch Sweeper] Cycle #{iteration}: no wallets found")
                
        except Exception as e:
            logger.error(f"[Batch Sweeper] Error in cycle #{iteration}: {e}")
        
        # Пауза между циклами
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_batch_sweeper_loop())
