"""
Настройка async сессии SQLAlchemy.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
    AsyncEngine,
)

from src.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Получить engine (создаётся лениво)."""
    settings = get_settings()

    # Определяем параметры в зависимости от типа БД
    engine_kwargs: dict = {
        "echo": settings.debug,
        "pool_pre_ping": True,
    }

    # SQLite не поддерживает pool_size и max_overflow
    if not settings.database_url.startswith("sqlite"):
        engine_kwargs.update(
            {
                "pool_size": 10,
                "max_overflow": 20,
                "pool_recycle": 3600,
                "pool_timeout": 30,  # Timeout для получения соединения из пула
            }
        )

        # Добавляем connect timeout для PostgreSQL
        if settings.database_url.startswith("postgresql"):
            engine_kwargs["connect_args"] = {
                "timeout": 10,  # Connection timeout в секундах
                "command_timeout": 60,  # Query timeout
            }

    return create_async_engine(settings.database_url, **engine_kwargs)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Получить фабрику сессий (создаётся лениво)."""
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency для получения сессии БД.
    Используется в FastAPI endpoints.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Контекстный менеджер для получения сессии БД.
    Используется в воркерах и фоновых задачах.
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """
    Инициализация БД (создание таблиц).
    В production используйте alembic migrations.
    """
    from src.db.models import Base

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Закрытие соединений с БД."""
    await get_engine().dispose()
