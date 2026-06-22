"""
Модели платежей: DepositAddress, PaymentSession, OnchainTx.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import (
    Base,
    UniversalBytes,
    UniversalJSON,
    UniversalUUID,
    UUIDMixin,
)
from src.db.models.enums import (
    DepositAddressLeaseStatus,
    PaymentSessionStatus,
    TxStatus,
    enum_values,
)

if TYPE_CHECKING:
    from src.db.models.invoice import Invoice


class DepositAddress(Base, UUIDMixin):
    """
    Депозитный адрес для приёма платежей.
    Приватный ключ хранится в зашифрованном виде.
    """

    __tablename__ = "deposit_addresses"

    # Адрес кошелька
    # - EVM: 42 символа (0x + 40 hex)
    # - Solana: 32-44 символа (base58)
    # - TON: 48 символов (user-friendly) или 66 (raw)
    address: Mapped[str] = mapped_column(String(70), unique=True, nullable=False)

    # Зашифрованный приватный ключ (AES-256-GCM)
    encrypted_privkey: Mapped[bytes] = mapped_column(UniversalBytes, nullable=False)

    # Группа сетей: 'evm' | 'solana' | 'ton'
    chain_group: Mapped[str] = mapped_column(String(20), default="evm", nullable=False)

    # HD wallet derivation info
    derivation_path: Mapped[str] = mapped_column(String(50), nullable=False)
    derivation_index: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)

    # Legacy флаг использования. Новая логика использует lease_status.
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    lease_status: Mapped[DepositAddressLeaseStatus] = mapped_column(
        Enum(
            DepositAddressLeaseStatus,
            name="deposit_address_lease_status",
            values_callable=enum_values(DepositAddressLeaseStatus),
        ),
        default=DepositAddressLeaseStatus.AVAILABLE,
        nullable=False,
        index=True,
    )
    leased_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_deposit_addr_available", "chain_group", "is_used"),
        Index("idx_deposit_addr_lease", "chain_group", "lease_status"),
    )


class PaymentSession(Base, UUIDMixin):
    """
    Сессия оплаты — связь инвойса с выбранной сетью и адресом.
    Создаётся когда плательщик выбирает сеть и токен.
    """

    __tablename__ = "payment_sessions"

    # Связь с инвойсом
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Выбранная сеть и токен
    chain: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # 'base' | 'arbitrum' | 'bsc'
    token: Mapped[str] = mapped_column(String(10), nullable=False)  # 'USDT' | 'USDC'

    # Назначенный депозитный адрес
    deposit_address_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("deposit_addresses.id"),
        nullable=False,
        index=True,
    )

    status: Mapped[PaymentSessionStatus] = mapped_column(
        Enum(
            PaymentSessionStatus,
            name="payment_session_status",
            values_callable=enum_values(PaymentSessionStatus),
        ),
        default=PaymentSessionStatus.PENDING,
        nullable=False,
        index=True,
    )

    # Когда была выбрана сеть
    chosen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    invoice: Mapped["Invoice"] = relationship(
        "Invoice", back_populates="payment_sessions"
    )
    deposit_address: Mapped["DepositAddress"] = relationship("DepositAddress")
    onchain_txs: Mapped[list["OnchainTx"]] = relationship(
        "OnchainTx",
        back_populates="payment_session",
        cascade="all, delete-orphan",
    )
    # Sweep job теперь в UnifiedSweepJob (source='invoice', source_id=payment_session.id)

    __table_args__ = (
        # Один инвойс — одна сессия на chain+token
        UniqueConstraint(
            "invoice_id", "chain", "token", name="uq_session_invoice_chain_token"
        ),
        Index("idx_session_address", "deposit_address_id"),
        Index("idx_session_status_expires", "status", "expires_at"),
        Index(
            "uq_payment_session_address_active",
            "deposit_address_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'seen_onchain')"),
            sqlite_where=text("status IN ('pending', 'seen_onchain')"),
        ),
    )


class AddressLeaseEvent(Base, UUIDMixin):
    """Audit log for deposit address lease lifecycle."""

    __tablename__ = "address_lease_events"

    deposit_address_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("deposit_addresses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    payment_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UniversalUUID(),
        ForeignKey("payment_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(UniversalJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    deposit_address: Mapped["DepositAddress"] = relationship("DepositAddress")
    payment_session: Mapped[Optional["PaymentSession"]] = relationship("PaymentSession")

    __table_args__ = (
        Index(
            "idx_address_lease_events_address_created",
            "deposit_address_id",
            "created_at",
        ),
        Index(
            "idx_address_lease_events_session_created",
            "payment_session_id",
            "created_at",
        ),
    )


class OnchainTx(Base, UUIDMixin):
    """
    Найденная onchain транзакция (ERC20 Transfer).
    """

    __tablename__ = "onchain_txs"

    # Сеть
    chain: Mapped[str] = mapped_column(String(20), nullable=False)

    # Данные транзакции
    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    log_index: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # Индекс лога в транзакции

    # Адреса
    from_address: Mapped[str] = mapped_column(String(42), nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)

    # Токен и сумма
    token_contract: Mapped[str] = mapped_column(String(42), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)

    # Связь с сессией оплаты
    payment_session_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("payment_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Статус и подтверждения
    status: Mapped[TxStatus] = mapped_column(
        Enum(TxStatus, name="tx_status", values_callable=enum_values(TxStatus)),
        default=TxStatus.PENDING,
        nullable=False,
        index=True,
    )
    confirmations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timestamps
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    payment_session: Mapped["PaymentSession"] = relationship(
        "PaymentSession",
        back_populates="onchain_txs",
    )

    __table_args__ = (
        # Уникальность по chain + tx_hash + log_index
        UniqueConstraint(
            "chain", "tx_hash", "log_index", name="uq_onchain_chain_tx_log"
        ),
        Index("idx_onchain_to_address", "to_address"),
        Index("idx_onchain_status", "status"),
    )
