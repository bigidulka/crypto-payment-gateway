"""Immutable on-chain receipt and durable signed transfer attempt."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, UniversalJSON, UniversalUUID


class OnchainReceipt(Base):
    __tablename__ = "onchain_receipts"
    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("merchants.id"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("invoices.id"), unique=True, nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("payment_sessions.id"), unique=True, nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("ledger_assets.id"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("ledger_transactions.id"), unique=True, nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    amount_atomic: Mapped[int] = mapped_column(Numeric(), nullable=False)
    evidence: Mapped[dict] = mapped_column(UniversalJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OnchainOperation(Base):
    __tablename__ = "onchain_operations"
    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("merchants.id"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    sender: Mapped[str] = mapped_column(String(128), nullable=False)
    destination: Mapped[str] = mapped_column(String(128), nullable=False)
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UniversalUUID(), ForeignKey("ledger_assets.id")
    )
    amount_atomic: Mapped[int] = mapped_column(Numeric(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing")
    tx_id: Mapped[str | None] = mapped_column(String(128))
    encrypted_payload: Mapped[str | None] = mapped_column(Text())
    prepared_context: Mapped[dict | None] = mapped_column(UniversalJSON())
    fee_budget_atomic: Mapped[int | None] = mapped_column(Numeric())
    fee_paid_atomic: Mapped[int | None] = mapped_column(Numeric())
    native_cost_atomic: Mapped[int | None] = mapped_column(Numeric())
    gas_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UniversalUUID(), ForeignKey("ledger_transactions.id"), unique=True
    )
    last_broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wallet_released: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UniversalUUID(), ForeignKey("ledger_transactions.id")
    )
    last_error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (UniqueConstraint("merchant_id", "key", name="uq_onchain_operation_key"),)


class OnchainScanCursor(Base):
    __tablename__ = "onchain_scan_cursors"
    session_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("payment_sessions.id"), primary_key=True
    )
    chain: Mapped[str] = mapped_column(String(32), nullable=False)
    last_block: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
