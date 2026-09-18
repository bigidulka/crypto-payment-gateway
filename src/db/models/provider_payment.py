"""Disabled internal provider-payment ingestion source facts.

No HTTP route, worker, or provider callback imports these models in this slice.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, UniversalJSON, UniversalUUID


class ProviderPaymentMapping(Base):
    """Pre-bound merchant invoice/provider identity; never inferred from a symbol."""

    __tablename__ = "provider_payment_mappings"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    rail_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    provider_account_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_network: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_invoice_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_asset_code: Mapped[str] = mapped_column(String(32), nullable=False)
    ledger_asset_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    expected_amount_atomic: Mapped[int] = mapped_column(Numeric(), nullable=False)
    receivable_account_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    payable_account_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "invoice_id"],
            ["invoices.merchant_id", "invoices.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "rail_id"], ["rails.merchant_id", "rails.id"], ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
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
        CheckConstraint(
            "length(provider_network) BETWEEN 1 AND 64", name="ck_provider_mapping_network"
        ),
        CheckConstraint(
            "length(provider_account_identity) BETWEEN 8 AND 128",
            name="ck_provider_mapping_account_identity",
        ),
        CheckConstraint(
            "CAST(expected_amount_atomic AS TEXT) <> 'NaN' AND expected_amount_atomic > 0 AND expected_amount_atomic < 100000000000000000000000000000000000000000000000000000000000000000000000000 AND expected_amount_atomic = trunc(expected_amount_atomic)",
            name="ck_provider_mapping_expected_atomic",
        ),
        UniqueConstraint(
            "provider_account_identity",
            "provider_network",
            "provider_invoice_id",
            name="uq_provider_mapping_identity",
        ),
        Index("ix_provider_mapping_invoice", "merchant_id", "invoice_id"),
    )


class ProviderPaymentObservation(Base):
    """Immutable canonical provider fact, globally deduped by account+invoice."""

    __tablename__ = "provider_payment_observations"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    mapping_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    rail_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    provider_account_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_network: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_invoice_id: Mapped[str] = mapped_column(String(256), nullable=False)
    ledger_asset_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    observed_amount_atomic: Mapped[int] = mapped_column(Numeric(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    economic_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "mapping_id"],
            ["provider_payment_mappings.merchant_id", "provider_payment_mappings.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
        CheckConstraint(
            "CAST(observed_amount_atomic AS TEXT) <> 'NaN' AND observed_amount_atomic > 0 AND observed_amount_atomic < 100000000000000000000000000000000000000000000000000000000000000000000000000 AND observed_amount_atomic = trunc(observed_amount_atomic)",
            name="ck_provider_observation_atomic",
        ),
        CheckConstraint(
            "length(economic_digest) = 64", name="ck_provider_observation_economic_digest"
        ),
        CheckConstraint(
            "length(evidence_digest) = 64", name="ck_provider_observation_evidence_digest"
        ),
        CheckConstraint(
            "source_kind IN ('provider_poll', 'provider_webhook')",
            name="ck_provider_observation_source",
        ),
        UniqueConstraint(
            "provider_account_identity",
            "provider_network",
            "provider_invoice_id",
            name="uq_provider_observation_identity",
        ),
    )


class ProviderPaymentReceipt(Base):
    """One immutable completed-projection proof per verified provider observation."""

    __tablename__ = "provider_payment_receipts"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    decision_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    ledger_transaction_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    invoice_event_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    intended_recipient_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "observation_id"],
            ["provider_payment_observations.merchant_id", "provider_payment_observations.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "decision_id"],
            ["provider_verification_decisions.merchant_id", "provider_verification_decisions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "ledger_transaction_id"],
            ["ledger_transactions.merchant_id", "ledger_transactions.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["merchant_id", "invoice_id"],
            ["invoices.merchant_id", "invoices.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["invoice_event_id"], ["invoice_events.id"], ondelete="RESTRICT"),
        UniqueConstraint("observation_id", name="uq_provider_receipt_observation"),
        UniqueConstraint("merchant_id", "id", name="uq_provider_receipt_tenant"),
        CheckConstraint("intended_recipient_count >= 0", name="ck_provider_receipt_intent_count"),
        CheckConstraint("length(event_id) = 64", name="ck_provider_receipt_event_id"),
        UniqueConstraint("event_id", name="uq_provider_receipt_event"),
    )


class ProviderPaymentRecipientIntent(Base):
    """Immutable delivery intention snapshot; delivery outcome does not affect payment proof."""

    __tablename__ = "provider_payment_recipient_intents"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    receipt_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    outbox_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    webhook_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    destination_url: Mapped[str] = mapped_column(String(500), nullable=False)
    payload_snapshot: Mapped[dict] = mapped_column(UniversalJSON, nullable=False)
    destination_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "receipt_id"],
            ["provider_payment_receipts.merchant_id", "provider_payment_receipts.id"],
            ondelete="RESTRICT",
        ),
        # Outbox/webhook are verified by the PostgreSQL insert trigger at the
        # posting instant, but deliberately are not FK parents: later delivery
        # deletion/reconfiguration cannot erase economic proof.
        CheckConstraint(
            "length(destination_digest) = 64", name="ck_provider_intent_destination_digest"
        ),
        CheckConstraint("length(payload_digest) = 64", name="ck_provider_intent_payload_digest"),
        UniqueConstraint("receipt_id", "outbox_id", name="uq_provider_intent_receipt_outbox"),
        UniqueConstraint("receipt_id", "webhook_id", name="uq_provider_intent_receipt_webhook"),
    )


class ProviderVerificationDecision(Base):
    """Append-only server verification decision; only verified decisions post the ledger."""

    __tablename__ = "provider_verification_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    observation_id: Mapped[uuid.UUID] = mapped_column(UniversalUUID(), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["observation_id"], ["provider_payment_observations.id"], ondelete="RESTRICT"
        ),
        CheckConstraint(
            "decision IN ('verified', 'reconciliation_required', 'rejected')",
            name="ck_provider_decision_value",
        ),
        UniqueConstraint("observation_id", name="uq_provider_decision_observation"),
        UniqueConstraint("merchant_id", "id", name="uq_provider_decision_tenant"),
        Index("ix_provider_decision_observation", "observation_id"),
    )
