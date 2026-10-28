"""Durable provider identities, invoice choices and never-retried money operations."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, UniversalJSON, UniversalUUID
from src.ledger.amounts import ATOMIC_AMOUNT_CHECK_SQL


class GatewayRailConnection(Base):
    __tablename__ = "gateway_rail_connections"

    rail_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    rail_type: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    account_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    credentials_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    receivable_account_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    payable_account_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "rail_id"], ["rails.merchant_id", "rails.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["merchant_id", "receivable_account_id"],
            ["ledger_accounts.merchant_id", "ledger_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "payable_account_id"],
            ["ledger_accounts.merchant_id", "ledger_accounts.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "rail_type", "network", "account_identity", name="uq_gateway_provider_account"
        ),
        CheckConstraint(
            "rail_type IN ('cryptobot', 'xrocket', 'tgwallet')", name="ck_gateway_connection_type"
        ),
        CheckConstraint(
            "length(account_identity)=64 AND length(credentials_digest)=64",
            name="ck_gateway_connection_digests",
        ),
    )


class ProviderSettlement(Base):
    """Immutable net settlement linked to the existing immutable gross fact."""

    __tablename__ = "provider_settlements"

    observation_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    settled_amount_atomic: Mapped[int] = mapped_column(Numeric(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "observation_id"],
            ["provider_payment_observations.merchant_id", "provider_payment_observations.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            ATOMIC_AMOUNT_CHECK_SQL.replace("amount_atomic", "settled_amount_atomic"),
            name="ck_provider_settlement_amount",
        ),
    )


class GatewayInvoiceOptions(Base):
    __tablename__ = "gateway_invoice_options"

    invoice_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    allowed_rails: Mapped[list[str]] = mapped_column(UniversalJSON(), nullable=False)
    customer_telegram_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "invoice_id"],
            ["invoices.merchant_id", "invoices.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(request_digest)=64", name="ck_gateway_invoice_digest"),
    )


class GatewayOperation(Base):
    __tablename__ = "gateway_operations"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    rail_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UniversalUUID(), nullable=True)
    ledger_asset_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    amount_atomic: Mapped[int] = mapped_column(Numeric(), nullable=False)
    destination: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    destination_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="telegram_user_id"
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitting")
    provider_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pay_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(UniversalUUID(), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "rail_id"], ["rails.merchant_id", "rails.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["rail_id"], ["gateway_rail_connections.rail_id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["merchant_id", "invoice_id"],
            ["invoices.merchant_id", "invoices.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["mapping_id"], ["provider_payment_mappings.id"], ondelete="RESTRICT"),
        UniqueConstraint(
            "merchant_id", "operation_type", "idempotency_key", name="uq_gateway_operation_request"
        ),
        UniqueConstraint("invoice_id", name="uq_gateway_invoice_operation"),
        CheckConstraint("operation_type IN ('invoice','payout')", name="ck_gateway_operation_type"),
        CheckConstraint(
            "status IN ('submitting','pending','unknown','completed',"
            "'failed','reconciliation_required')",
            name="ck_gateway_operation_status",
        ),
        CheckConstraint(
            "(operation_type='invoice') = (invoice_id IS NOT NULL)",
            name="ck_gateway_operation_invoice",
        ),
        CheckConstraint(ATOMIC_AMOUNT_CHECK_SQL, name="ck_gateway_operation_amount"),
        CheckConstraint(
            "length(request_digest)=64 AND length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_gateway_operation_identity",
        ),
    )


class GatewayPayoutReceipt(Base):
    """Immutable evidence and exact journal proof for an issued payout."""

    __tablename__ = "gateway_payout_receipts"

    operation_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    provider_account_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(256), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ledger_transaction_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(["operation_id"], ["gateway_operations.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["merchant_id", "ledger_transaction_id"],
            ["ledger_transactions.merchant_id", "ledger_transactions.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "provider_account_identity",
            "provider_reference",
            name="uq_gateway_payout_provider_receipt",
        ),
        CheckConstraint(
            "length(provider_account_identity)=64 AND length(evidence_digest)=64 "
            "AND length(provider_reference)>0",
            name="ck_gateway_payout_evidence",
        ),
    )
