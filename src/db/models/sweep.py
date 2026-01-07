"""
Модели для sweep (вывод токенов) и webhook outbox.
"""

import enum
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

if TYPE_CHECKING:
    from src.db.models.merchant import Webhook
    from src.db.models.payment import PaymentSession


class SweepState(str, enum.Enum):
    """Состояние sweep job."""

    PENDING_GAS = "pending_gas"  # Ожидает проверки газа
    FUNDING = "funding"  # Отправка газа на deposit address
    SWEEPING = "sweeping"  # Вывод токенов на treasury
    COMPLETED = "completed"  # Успешно завершено
    FAILED = "failed"  # Ошибка


class SweepJob(Base, UUIDMixin, TimestampMixin):
    """
    Задача на вывод токенов с deposit address на treasury.
    """

    __tablename__ = "sweep_jobs"

    # Связь с сессией оплаты
    payment_session_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("payment_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Состояние
    state: Mapped[SweepState] = mapped_column(
        Enum(SweepState, name="sweep_state"),
        default=SweepState.PENDING_GAS,
        nullable=False,
        index=True,
    )

    # Хеши транзакций
    gas_tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)
    sweep_tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)

    # Retry логика
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    payment_session: Mapped["PaymentSession"] = relationship(
        "PaymentSession",
        back_populates="sweep_job",
    )

    __table_args__ = (Index("idx_sweep_state_retry", "state", "next_retry_at"),)


class OutboxStatus(str, enum.Enum):
    """Статус webhook в outbox."""

    PENDING = "pending"  # Ожидает отправки
    SENT = "sent"  # Успешно отправлен
    FAILED = "failed"  # Все попытки исчерпаны


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
        Enum(OutboxStatus, name="outbox_status"),
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


class SystemLogLevel(str, enum.Enum):
    """Уровень лога."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


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
        Enum(SystemLogLevel, name="system_log_level"),
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
