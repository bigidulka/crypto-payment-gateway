"""
Redis connection pool для атомарных операций.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import redis.asyncio as aioredis
from redis.asyncio import Redis

from src.core.config import get_settings


_redis_pool: Optional[Redis] = None


async def get_redis() -> Redis:
    """
    Получить Redis клиент из пула соединений.

    Использует lazy initialization с singleton pattern.
    """
    global _redis_pool

    if _redis_pool is None:
        settings = get_settings()
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )

    return _redis_pool


async def close_redis() -> None:
    """Закрыть Redis соединения."""
    global _redis_pool

    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


@asynccontextmanager
async def redis_lock(
    key: str,
    timeout: int = 30,
    blocking_timeout: float = 0.1,
) -> AsyncGenerator[bool, None]:
    """
    Distributed lock на основе Redis SETNX.

    Args:
        key: Ключ блокировки
        timeout: TTL блокировки в секундах
        blocking_timeout: Время ожидания получения блокировки

    Yields:
        True если блокировка получена, False если нет

    Usage:
        async with redis_lock("sweep:job:123") as acquired:
            if acquired:
                # Do work
                pass
    """
    redis = await get_redis()
    lock_key = f"lock:{key}"
    acquired = False

    try:
        # Пытаемся получить блокировку с NX (set if not exists) и EX (expire)
        acquired = await redis.set(lock_key, "1", nx=True, ex=timeout)
        yield bool(acquired)
    finally:
        if acquired:
            # Освобождаем блокировку
            await redis.delete(lock_key)


async def try_acquire_lock(key: str, timeout: int = 30) -> bool:
    """
    Попробовать получить блокировку без ожидания.

    Args:
        key: Ключ блокировки
        timeout: TTL блокировки в секундах

    Returns:
        True если блокировка получена
    """
    redis = await get_redis()
    lock_key = f"lock:{key}"
    result = await redis.set(lock_key, "1", nx=True, ex=timeout)
    return bool(result)


async def release_lock(key: str) -> None:
    """Освободить блокировку."""
    redis = await get_redis()
    lock_key = f"lock:{key}"
    await redis.delete(lock_key)
