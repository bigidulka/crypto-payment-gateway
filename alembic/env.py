"""
Alembic environment configuration.
Поддержка async SQLAlchemy.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from src.core.config import get_settings
from src.db.models import Base

# Alembic Config object
config = context.config

# Логирование
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata для autogenerate
target_metadata = Base.metadata

# Alembic uses MIGRATION_DATABASE_URL when provided, separate from the runtime
# DATABASE_URL after a reviewed non-owner application-role rollout.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.get_migration_database_url())


def _migration_timeout_ms(name: str) -> int | None:
    """Read a bounded migration-owner timeout without affecting runtime config."""
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer millisecond value") from exc
    if not 1_000 <= value <= 300_000:
        raise RuntimeError(f"{name} must be between 1000 and 300000 milliseconds")
    return value


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Generates SQL script without connecting to DB.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations in one transaction with optional bounded DDL timeouts."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        for setting in ("MIGRATION_LOCK_TIMEOUT_MS", "MIGRATION_STATEMENT_TIMEOUT_MS"):
            timeout_ms = _migration_timeout_ms(setting)
            if timeout_ms is not None:
                parameter = (
                    "lock_timeout"
                    if setting == "MIGRATION_LOCK_TIMEOUT_MS"
                    else "statement_timeout"
                )
                connection.execute(text(f"SET LOCAL {parameter} = '{timeout_ms}ms'"))
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations in 'online' mode with async engine.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
