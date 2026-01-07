"""
Модели для Persistent Deposit Addresses.

Позволяет выделять постоянные адреса пользователям для пополнения.
Каждый пользователь получает уникальный адрес в каждой сети.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import Base

if TYPE_CHECKING:
    from src.db.models.merchant import Merchant


class DepositStatus(str, Enum):
    """Статус депозита."""

    PENDING = "pending"  # Обнаружен, ждём подтверждений
    CONFIRMING = "confirming"  # Набираем подтверждения
    CONFIRMED = "confirmed"  # Подтверждён, зачислен на баланс
    SWEPT = "swept"  # Средства переведены в treasury


class UserWallet(Base):
    """
    Кошелёк пользователя (группа адресов во всех сетях).

    Связывает внешний ID пользователя (из бота/сайта мерчанта)
    с набором deposit адресов в каждой сети.
    """

    __tablename__ = "user_wallets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # Связь с мерчантом
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Внешний ID пользователя (telegram_id, user_id из сайта и т.д.)
    external_user_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Опциональные метаданные
    user_metadata: Mapped[dict | None] = mapped_column(
        JSON,
        default=None,
        nullable=True,
    )

    # Флаг активности
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    merchant: Mapped["Merchant"] = relationship(
        back_populates="user_wallets",
        lazy="selectin",
    )

    addresses: Mapped[list["WalletAddress"]] = relationship(
        back_populates="user_wallet",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    deposits: Mapped[list["Deposit"]] = relationship(
        back_populates="user_wallet",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    balances: Mapped[list["UserBalance"]] = relationship(
        back_populates="user_wallet",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        # Уникальность: один кошелёк на пользователя у мерчанта
        UniqueConstraint(
            "merchant_id", "external_user_id", name="uq_user_wallet_merchant_user"
        ),
        Index("ix_user_wallet_merchant_active", "merchant_id", "is_active"),
    )


class WalletAddress(Base):
    """
    Адрес кошелька в конкретной сети.

    Каждый UserWallet имеет по одному адресу в каждой поддерживаемой сети.
    """

    __tablename__ = "wallet_addresses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_wallets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Сеть (arbitrum, base, bsc, polygon, avax, optimism)
    chain: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    # Deposit адрес
    address: Mapped[str] = mapped_column(
        String(42),
        nullable=False,
        index=True,
    )

    # HD Wallet derivation index
    derivation_index: Mapped[int] = mapped_column(
        nullable=False,
    )

    # Зашифрованный приватный ключ (AES-256-GCM)
    encrypted_private_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )

    # Флаг активности отслеживания
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Последний отсканированный блок для этого адреса
    last_scanned_block: Mapped[int | None] = mapped_column(
        default=None,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    user_wallet: Mapped["UserWallet"] = relationship(
        back_populates="addresses",
    )

    deposits: Mapped[list["Deposit"]] = relationship(
        back_populates="wallet_address",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    __table_args__ = (
        # Уникальность: один адрес на кошелёк в сети
        UniqueConstraint("user_wallet_id", "chain", name="uq_wallet_address_chain"),
        # Уникальность адреса в сети
        UniqueConstraint("chain", "address", name="uq_chain_address"),
        Index("ix_wallet_address_chain_active", "chain", "is_active"),
    )


class Deposit(Base):
    """
    Запись о депозите (поступлении средств).

    Создаётся при обнаружении Transfer на wallet address.
    """

    __tablename__ = "deposits"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_wallets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    wallet_address_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallet_addresses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Блокчейн данные
    chain: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    tx_hash: Mapped[str] = mapped_column(
        String(66),
        nullable=False,
        index=True,
    )

    block_number: Mapped[int] = mapped_column(
        nullable=False,
    )

    log_index: Mapped[int] = mapped_column(
        nullable=False,
    )

    # Сумма и токен
    amount: Mapped[Decimal] = mapped_column(
        Numeric(36, 18),
        nullable=False,
    )

    asset: Mapped[str] = mapped_column(
        String(10),
        nullable=False,  # USDT, USDC
    )

    token_contract: Mapped[str] = mapped_column(
        String(42),
        nullable=False,
    )

    # Отправитель
    from_address: Mapped[str] = mapped_column(
        String(42),
        nullable=False,
    )

    # Статус
    status: Mapped[DepositStatus] = mapped_column(
        SqlEnum(DepositStatus, name="deposit_status"),
        default=DepositStatus.PENDING,
        nullable=False,
        index=True,
    )

    # Подтверждения
    confirmations: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
    )

    required_confirmations: Mapped[int] = mapped_column(
        nullable=False,
    )

    # Timestamps
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    credited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Sweep данные
    sweep_tx_hash: Mapped[str | None] = mapped_column(
        String(66),
        nullable=True,
    )

    swept_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    user_wallet: Mapped["UserWallet"] = relationship(
        back_populates="deposits",
    )

    wallet_address: Mapped["WalletAddress"] = relationship(
        back_populates="deposits",
    )

    __table_args__ = (
        # Уникальность транзакции
        UniqueConstraint("chain", "tx_hash", "log_index", name="uq_deposit_tx"),
        Index("ix_deposit_status_chain", "status", "chain"),
    )


class UserBalance(Base):
    """
    Баланс пользователя по активу.

    Обновляется при подтверждении депозитов.
    """

    __tablename__ = "user_balances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    user_wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_wallets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Актив (USDT, USDC)
    asset: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    # Баланс
    balance: Mapped[Decimal] = mapped_column(
        Numeric(36, 18),
        default=Decimal("0"),
        nullable=False,
    )

    # Общая сумма депозитов
    total_deposited: Mapped[Decimal] = mapped_column(
        Numeric(36, 18),
        default=Decimal("0"),
        nullable=False,
    )

    # Общая сумма выводов
    total_withdrawn: Mapped[Decimal] = mapped_column(
        Numeric(36, 18),
        default=Decimal("0"),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    user_wallet: Mapped["UserWallet"] = relationship(
        back_populates="balances",
    )

    __table_args__ = (
        # Один баланс на актив
        UniqueConstraint("user_wallet_id", "asset", name="uq_user_balance_asset"),
    )
