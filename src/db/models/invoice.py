"""
Модели инвойса и событий.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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
    UniversalArray,
    UniversalJSON,
    UniversalUUID,
)

if TYPE_CHECKING:
    from src.db.models.merchant import Merchant
    from src.db.models.payment import PaymentSession

from src.db.models.enums import InvoiceStatus, enum_values


class Invoice(Base, UUIDMixin, TimestampMixin):
    """
    Инвойс — запрос на оплату от мерчанта.
    """

    __tablename__ = "invoices"

    # Публичный ID для URL (короткий, user-friendly)
    public_id: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        index=True,
    )

    # Связь с мерчантом
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Сумма и актив
    # Decimal(36, 18) для поддержки больших сумм с высокой точностью
    amount: Mapped[Decimal] = mapped_column(
        Numeric(36, 18),
        nullable=False,
    )
    asset: Mapped[str] = mapped_column(String(10), nullable=False)  # 'USDT' | 'USDC'

    # Разрешённые сети для оплаты
    allowed_chains: Mapped[List[str]] = mapped_column(
        UniversalArray(String(20)),
        nullable=False,
    )

    # Статус
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status", values_callable=enum_values(InvoiceStatus)),
        default=InvoiceStatus.CREATED,
        nullable=False,
        index=True,
    )

    # TTL и истечение
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Метаданные от мерчанта (order_id, comment и т.д.)
    extra_data: Mapped[Optional[dict[str, Any]]] = mapped_column(
        UniversalJSON, nullable=True
    )

    # Idempotency key для защиты от дублей
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Relationships
    merchant: Mapped["Merchant"] = relationship("Merchant", back_populates="invoices")
    payment_sessions: Mapped[List["PaymentSession"]] = relationship(
        "PaymentSession",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )
    events: Mapped[List["InvoiceEvent"]] = relationship(
        "InvoiceEvent",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceEvent.created_at",
    )

    # Constraints
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "idempotency_key",
            name="uq_invoice_idempotency",
        ),
        Index("idx_invoices_status_expires", "status", "expires_at"),
        Index("idx_invoices_merchant_created", "merchant_id", "created_at"),
    )

    @property
    def is_expired(self) -> bool:
        """Проверка, истёк ли инвойс."""
        # Если expires_at timezone-naive (из SQLite), считаем что это UTC
        if self.expires_at.tzinfo is None:
            from datetime import timezone

            expires_at_utc = self.expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at_utc = self.expires_at
        return datetime.now(timezone.utc) > expires_at_utc

    @property
    def is_payable(self) -> bool:
        """Можно ли оплатить инвойс."""
        return (
            self.status in (InvoiceStatus.CREATED, InvoiceStatus.AWAITING_PAYMENT)
            and not self.is_expired
        )


class InvoiceEvent(Base, UUIDMixin):
    """
    История событий инвойса для аудита.
    """

    __tablename__ = "invoice_events"

    # Связь с инвойсом
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Тип события
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Данные события
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(
        UniversalJSON, nullable=True
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    invoice: Mapped["Invoice"] = relationship("Invoice", back_populates="events")

    __table_args__ = (Index("idx_events_invoice_created", "invoice_id", "created_at"),)
