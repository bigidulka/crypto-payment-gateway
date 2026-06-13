"""
Legacy Sweeper - перенаправляет на Unified Sweeper.

Этот модуль устарел. Используйте unified_sweeper.py напрямую.
Оставлен для обратной совместимости.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_sweeper() -> None:
    """
    Запускает Unified Sweeper.
    
    Старый sweeper больше не используется - все sweep jobs
    теперь обрабатываются unified_sweeper через UnifiedSweepJob.
    """
    from src.workers.unified_sweeper import run_unified_sweeper
    
    logger.warning(
        "Legacy sweeper.run_sweeper() called - redirecting to unified_sweeper. "
        "Consider updating your docker-compose to use unified_sweeper_runner directly."
    )
    
    await run_unified_sweeper(interval_sec=30)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_sweeper())
