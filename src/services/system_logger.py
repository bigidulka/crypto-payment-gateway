"""
System Logging Service.
Логирование системных событий в БД для админ панели.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SystemLog, SystemLogLevel

logger = logging.getLogger(__name__)


class SystemLogger:
    """
    Сервис для записи системных логов в БД.
    Используется для отслеживания ошибок RPC, sweeper, poller и т.д.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(
        self,
        level: SystemLogLevel,
        source: str,
        message: str,
        chain: str | None = None,
        details: dict[str, Any] | None = None,
        invoice_id: UUID | None = None,
        sweep_id: UUID | None = None,
        tx_hash: str | None = None,
    ) -> SystemLog:
        """Записать лог в БД."""
        log_entry = SystemLog(
            level=level,
            source=source,
            message=message,
            chain=chain,
            details=details,
            invoice_id=invoice_id,
            sweep_id=sweep_id,
            tx_hash=tx_hash,
        )
        self.session.add(log_entry)
        await self.session.flush()
        return log_entry

    async def info(
        self,
        source: str,
        message: str,
        **kwargs,
    ) -> SystemLog:
        """Записать INFO лог."""
        return await self.log(SystemLogLevel.INFO, source, message, **kwargs)

    async def warning(
        self,
        source: str,
        message: str,
        **kwargs,
    ) -> SystemLog:
        """Записать WARNING лог."""
        return await self.log(SystemLogLevel.WARNING, source, message, **kwargs)

    async def error(
        self,
        source: str,
        message: str,
        **kwargs,
    ) -> SystemLog:
        """Записать ERROR лог."""
        return await self.log(SystemLogLevel.ERROR, source, message, **kwargs)

    async def critical(
        self,
        source: str,
        message: str,
        **kwargs,
    ) -> SystemLog:
        """Записать CRITICAL лог."""
        return await self.log(SystemLogLevel.CRITICAL, source, message, **kwargs)

    async def get_logs(
        self,
        level: SystemLogLevel | None = None,
        source: str | None = None,
        chain: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SystemLog], int]:
        """Получить логи с фильтрами."""
        stmt = select(SystemLog)

        if level:
            stmt = stmt.where(SystemLog.level == level)
        if source:
            stmt = stmt.where(SystemLog.source == source)
        if chain:
            stmt = stmt.where(SystemLog.chain == chain)
        if date_from:
            stmt = stmt.where(SystemLog.timestamp >= date_from)
        if date_to:
            stmt = stmt.where(SystemLog.timestamp <= date_to)

        # Count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = await self.session.scalar(count_stmt) or 0

        # Paginate
        stmt = stmt.order_by(desc(SystemLog.timestamp)).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        logs = result.scalars().all()

        return list(logs), total

    async def get_error_counts(
        self,
        hours: int = 24,
    ) -> dict[str, int]:
        """Получить количество ошибок по источникам за последние N часов."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        stmt = (
            select(SystemLog.source, func.count())
            .where(SystemLog.timestamp >= cutoff)
            .where(SystemLog.level.in_([SystemLogLevel.ERROR, SystemLogLevel.CRITICAL]))
            .group_by(SystemLog.source)
        )
        result = await self.session.execute(stmt)
        return {row[0]: row[1] for row in result.all()}

    async def cleanup_old_logs(self, days: int = 30) -> int:
        """Удалить логи старше N дней."""
        from sqlalchemy import delete
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = delete(SystemLog).where(SystemLog.timestamp < cutoff)
        result = await self.session.execute(stmt)
        return result.rowcount


# Глобальный helper для записи логов без контекста сессии
async def log_system_event(
    level: str,
    source: str,
    message: str,
    **kwargs,
) -> None:
    """
    Записать системное событие в БД.
    Использовать когда нет доступа к сессии напрямую.
    """
    from src.db.session import get_session_context

    try:
        async with get_session_context() as session:
            sys_logger = SystemLogger(session)
            level_enum = SystemLogLevel(level.lower())
            await sys_logger.log(level_enum, source, message, **kwargs)
            await session.commit()
    except Exception as e:
        # Fallback на обычный logger
        logger.error(f"Failed to write system log: {e}")
        logger.log(
            logging.getLevelName(level.upper()),
            f"[{source}] {message}",
        )
