"""Рельсы: общие подключённые платёжные провайдеры мерчантов.

Revision ID: 0009_merchant_rails
Revises: 0008_payment_amount_mismatch
Create Date: 2026-09-04

Платёжный шлюз до этого знал только on-chain приём через собственную
EVM-инфраструктуру. Таблица rails - точка подключения остальных способов
оплаты: кастодиальных API (CryptoBot, xRocket, TG Wallet) с кредами
мерчанта и будущих сетей (Solana, TON).
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_merchant_rails"
down_revision = "0008_payment_amount_mismatch"


def upgrade() -> None:
    op.create_table(
        "rails",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "merchant_id",
            sa.UUID(),
            sa.ForeignKey("merchants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rail_type", sa.String(32), nullable=False),
        sa.Column("network", sa.String(64), nullable=True),
        sa.Column("encrypted_credentials", sa.Text(), nullable=True),
        sa.Column("assets", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_balance_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rails_merchant_id", "rails", ["merchant_id"])
    op.create_index("ix_rails_rail_type", "rails", ["rail_type"])


def downgrade() -> None:
    op.drop_index("ix_rails_rail_type", table_name="rails")
    op.drop_index("ix_rails_merchant_id", table_name="rails")
    op.drop_table("rails")
