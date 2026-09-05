"""
Настройка async сессии SQLAlchemy для PostgreSQL.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Получить engine (создаётся лениво)."""
    settings = get_settings()

    # PostgreSQL оптимизированные настройки
    engine_kwargs: dict = {
        "echo": settings.debug,
        "pool_pre_ping": True,
        "pool_size": 20,  # Базовый размер пула
        "max_overflow": 30,  # Дополнительные соединения при пиковой нагрузке
        "pool_recycle": 1800,  # Пересоздание соединений каждые 30 минут
        "pool_timeout": 30,  # Timeout для получения соединения из пула
    }

    # Добавляем connect timeout для PostgreSQL
    runtime_database_url = settings.get_runtime_database_url()
    if runtime_database_url.startswith("postgresql"):
        engine_kwargs["connect_args"] = {
            "timeout": 10,  # Connection timeout в секундах
            "command_timeout": 60,  # Query timeout
        }

    engine = create_async_engine(runtime_database_url, **engine_kwargs)

    return engine


_RUNTIME_PROTECTED_TABLES = (
    "address_lease_events",
    "api_keys",
    "chain_checkpoints",
    "deposit_addresses",
    "deposits",
    "invoice_events",
    "invoices",
    "ledger_accounts",
    "ledger_assets",
    "ledger_entries",
    "ledger_transactions",
    "merchants",
    "onchain_txs",
    "outbox_webhooks",
    "payment_sessions",
    "rails",
    "unified_sweep_jobs",
    "user_balances",
    "user_wallets",
    "wallet_addresses",
    "webhooks",
)


async def verify_limited_runtime_database_role() -> None:
    """Fail closed before any runtime database work under limited-role mode."""
    settings = get_settings()
    if not settings.database_runtime_role_enabled:
        return
    settings.validate_limited_runtime_environment()
    async with get_engine().connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT current_user, current_setting('search_path'), rolsuper, "
                    "rolcreaterole, rolcreatedb, rolinherit, rolreplication, rolbypassrls, "
                    "EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=current_user), "
                    "EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=current_database() AND r.rolname=current_user), "
                    "EXISTS (SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE r.rolname=current_user), "
                    "EXISTS (SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname=current_user), "
                    "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname=current_user), "
                    "has_database_privilege(current_user, current_database(), 'CREATE'), "
                    "has_schema_privilege(current_user, 'public', 'CREATE'), "
                    "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege(current_user, p.oid, 'EXECUTE')), "
                    "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r', 'p') AND c.relname = ANY(:protected_tables) AND has_table_privilege(current_user, c.oid, 'TRUNCATE')) "
                    "FROM pg_roles WHERE rolname=current_user"
                ),
                {"protected_tables": list(_RUNTIME_PROTECTED_TABLES)},
            )
        ).one()
        role, search_path, *guard_flags = row
        if any(guard_flags):
            raise RuntimeError(
                f"limited runtime database role {role!r} has elevated ownership or DDL privilege"
            )
        paths = [part.strip().strip('"') for part in search_path.split(",")]
        if paths[:2] != ["public", "pg_catalog"]:
            raise RuntimeError("limited runtime database search_path must be public, pg_catalog")


async def require_runtime_database_ready() -> None:
    """Perform the real database half of runtime readiness without schema DDL."""
    await verify_limited_runtime_database_role()
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


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
    """Development-only schema bootstrap; forbidden for limited runtime roles."""
    settings = get_settings()
    if settings.database_runtime_role_enabled:
        raise RuntimeError("init_db is forbidden when DATABASE_RUNTIME_ROLE_ENABLED=true")
    from src.db.models import Base

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Закрытие соединений с БД."""
    await get_engine().dispose()
