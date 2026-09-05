"""Append-only multi-tenant ledger foundation; not wired to payment flows yet."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, UniversalUUID
from src.ledger.amounts import ATOMIC_AMOUNT_CHECK_SQL


class LedgerAccountType(str, Enum):
    GATEWAY_TREASURY_PENDING = "gateway_treasury_pending"
    MERCHANT_PAYABLE = "merchant_payable"
    MERCHANT_CUSTODIAL_RECEIVABLE = "merchant_custodial_receivable"
    SUSPENSE_UNMATCHED = "suspense_unmatched"
    REFUND_LIABILITY = "refund_liability"
    MERCHANT_FEE_REVENUE = "merchant_fee_revenue"
    PROVIDER_FEE_EXPENSE = "provider_fee_expense"
    NETWORK_FEE_EXPENSE = "network_fee_expense"
    PROVIDER_EXPENSE = "provider_expense"


class LedgerDirection(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class LedgerTransactionStatus(str, Enum):
    OPEN = "open"
    POSTED = "posted"


class LedgerAsset(Base):
    __tablename__ = "ledger_assets"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    network_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    network_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    is_native: Mapped[bool] = mapped_column(nullable=False, default=False)
    atomic_decimals: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "atomic_decimals >= 0 AND atomic_decimals <= 255", name="ck_ledger_asset_decimals"
        ),
        UniqueConstraint(
            "network_kind",
            "network_identifier",
            "canonical_identifier",
            "is_native",
            name="uq_ledger_asset_canonical",
        ),
    )


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    account_type: Mapped[str] = mapped_column(String(64), nullable=False)
    custody_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        UniqueConstraint("merchant_id", "id", name="uq_ledger_account_tenant"),
    )


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LedgerTransactionStatus.OPEN.value
    )
    source_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    source_external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    posting_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(status = 'posted') = (posted_at IS NOT NULL)", name="ck_ledger_transaction_posted_at"
        ),
        ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        UniqueConstraint("merchant_id", "id", name="uq_ledger_transaction_tenant"),
        UniqueConstraint(
            "merchant_id", "idempotency_key", name="uq_ledger_transaction_idempotency"
        ),
        UniqueConstraint(
            "source_namespace", "source_external_id", name="uq_ledger_transaction_source"
        ),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    ledger_transaction_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    ledger_asset_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount_atomic: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "ledger_transaction_id"],
            ["ledger_transactions.merchant_id", "ledger_transactions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "ledger_account_id"],
            ["ledger_accounts.merchant_id", "ledger_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
        CheckConstraint("direction IN ('debit','credit')", name="ck_ledger_entry_direction"),
        CheckConstraint(ATOMIC_AMOUNT_CHECK_SQL, name="ck_ledger_entry_atomic"),
    )
