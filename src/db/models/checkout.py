"""Merchant Telegram checkout profile and append-only bot credit acknowledgements."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, UniversalJSON, UniversalUUID


class CheckoutProfile(Base):
    __tablename__ = "checkout_profiles"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("merchants.id"), primary_key=True
    )
    profile: Mapped[dict] = mapped_column(UniversalJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CheckoutPaymentAck(Base):
    __tablename__ = "checkout_payment_acks"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("invoices.id"), primary_key=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(), ForeignKey("merchants.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
