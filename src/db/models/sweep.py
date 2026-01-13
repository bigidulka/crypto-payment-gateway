"""
Модели для webhook outbox и системных логов.

Старые модели SweepJob и DepositSweepJob удалены.
Используйте UnifiedSweepJob из unified_sweep.py.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import (
    Base,
    TimestampMixin,
    UUIDMixin,
    UniversalJSON,
    UniversalUUID,
)
from src.db.models.enums import (
    OutboxStatus,
    SystemLogLevel,
    enum_values,
)

if TYPE_CHECKING:
    from src.db.models.merchant import Webhook


class OutboxWebhook(Base, UUIDMixin):
    """
    Outbox для надёжной доставки webhooks.
    Паттерн Transactional Outbox.
    """

    __tablename__ = "outbox_webhooks"

    # Связи
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("webhooks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Данные события
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(UniversalJSON, nullable=False)

    # Retry логика
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Статус
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status", values_callable=enum_values(OutboxStatus)),
        default=OutboxStatus.PENDING,
        nullable=False,
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    webhook: Mapped["Webhook"] = relationship("Webhook", lazy="joined")

    __table_args__ = (Index("idx_outbox_pending_retry", "status", "next_retry_at"),)


class ChainCheckpoint(Base, UUIDMixin):
    """
    Checkpoint сканирования блоков для каждой сети.
    """

    __tablename__ = "chain_checkpoints"

    # Сеть (уникальная)
    chain: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)

    # Последний обработанный блок
    last_scanned_block: Mapped[int] = mapped_column(Integer, nullable=False)

    # Timestamp обновления
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SystemLog(Base, UUIDMixin):
    """
    Системные логи для админ панели.
    Хранит ошибки RPC, sweeper, poller, webhook и т.д.
    """

    __tablename__ = "system_logs"

    # Timestamp
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Уровень лога
    level: Mapped[SystemLogLevel] = mapped_column(
        Enum(SystemLogLevel, name="system_log_level", values_callable=enum_values(SystemLogLevel)),
        nullable=False,
        index=True,
    )

    # Источник (poller, sweeper, webhook, rpc)
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Сеть (опционально)
    chain: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)

    # Сообщение
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Дополнительные данные (JSON)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(
        UniversalJSON, nullable=True
    )

    # Связанные сущности (опционально)
    invoice_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UniversalUUID(), nullable=True, index=True
    )
    sweep_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UniversalUUID(), nullable=True, index=True
    )
    tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)

    __table_args__ = (
        Index("idx_system_logs_timestamp_level", "timestamp", "level"),
        Index("idx_system_logs_source_chain", "source", "chain"),
    )
