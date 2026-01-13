"""
Модели мерчанта: Merchant, ApiKey, Webhook.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import (
    Base,
    TimestampMixin,
    UUIDMixin,
    UniversalArray,
    UniversalUUID,
)

if TYPE_CHECKING:
    from src.db.models.invoice import Invoice
    from src.db.models.user_wallet import UserWallet


class Merchant(Base, UUIDMixin, TimestampMixin):
    """
    Мерчант — владелец магазина/сервиса, использующий платёжный шлюз.
    """

    __tablename__ = "merchants"

    # Основная информация
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )

    # Статус
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    api_keys: Mapped[List["ApiKey"]] = relationship(
        "ApiKey",
        back_populates="merchant",
        cascade="all, delete-orphan",
    )
    webhooks: Mapped[List["Webhook"]] = relationship(
        "Webhook",
        back_populates="merchant",
        cascade="all, delete-orphan",
    )
    invoices: Mapped[List["Invoice"]] = relationship(
        "Invoice",
        back_populates="merchant",
        cascade="all, delete-orphan",
    )
    user_wallets: Mapped[List["UserWallet"]] = relationship(
        "UserWallet",
        back_populates="merchant",
        cascade="all, delete-orphan",
    )


class ApiKey(Base, UUIDMixin):
    """
    API ключ для аутентификации мерчанта.
    Хранится только хеш ключа, сам ключ показывается один раз при создании.
    """

    __tablename__ = "api_keys"

    # Связь с мерчантом
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Ключ (хранится хеш)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # Для идентификации в логах

    # Метаданные
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="Default")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    merchant: Mapped["Merchant"] = relationship("Merchant", back_populates="api_keys")


class Webhook(Base, UUIDMixin):
    """
    Настройки webhook для отправки событий мерчанту.
    """

    __tablename__ = "webhooks"

    # Связь с мерчантом
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # URL и секрет для подписи
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    secret: Mapped[str] = mapped_column(String(64), nullable=False)  # Для HMAC подписи

    # Какие события отправлять
    # ['invoice.created', 'invoice.seen_onchain', 'invoice.confirmed', 'invoice.expired']
    events: Mapped[List[str]] = mapped_column(
        UniversalArray(String(50)),
        nullable=False,
        default=list,
    )

    # Статус
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    merchant: Mapped["Merchant"] = relationship("Merchant", back_populates="webhooks")
