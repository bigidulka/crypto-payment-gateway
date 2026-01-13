"""
Unified Sweeper Runner.

Запускает единый sweep worker который обрабатывает UnifiedSweepJob из очереди.
"""

import asyncio
import logging
import sys
import os

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def main():
    """Entry point для unified sweeper."""
    from src.workers.unified_sweeper import run_unified_sweeper
    from src.core.config import get_settings
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger(__name__)
    
    settings = get_settings()
    
    # Интервал из настроек или 30 секунд по умолчанию
    interval = getattr(settings, 'sweep_interval_seconds', 30)
    
    logger.info(
        f"Starting Unified Sweeper (interval: {interval}s, "
        f"treasury: {settings.get_treasury_address('base')[:12]}...)"
    )
    
    asyncio.run(run_unified_sweeper(interval_sec=interval))


if __name__ == "__main__":
    main()
