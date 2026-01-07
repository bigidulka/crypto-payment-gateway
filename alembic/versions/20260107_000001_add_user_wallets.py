"""Add user wallets for persistent deposits

Revision ID: 20260107_000001
Revises: 20251225_000000_0001
Create Date: 2026-01-07

Добавляет таблицы для постоянных депозитных адресов:
- user_wallets: кошельки пользователей
- wallet_addresses: адреса в каждой сети
- deposits: записи о депозитах
- user_balances: балансы пользователей
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260107_000001"
down_revision = "20251225_000000_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create deposit_status enum
    deposit_status = postgresql.ENUM(
        "pending",
        "confirming",
        "confirmed",
        "swept",
        name="deposit_status",
        create_type=False,
    )
    deposit_status.create(op.get_bind(), checkfirst=True)

    # Create user_wallets table
    op.create_table(
        "user_wallets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_user_id", sa.String(255), nullable=False),
        sa.Column("user_metadata", postgresql.JSONB, nullable=True),
        sa.Column("is_active", sa.Boolean(), default=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index("ix_user_wallets_merchant_id", "user_wallets", ["merchant_id"])
    op.create_index(
        "ix_user_wallets_external_user_id", "user_wallets", ["external_user_id"]
    )
    op.create_index(
        "ix_user_wallet_merchant_active", "user_wallets", ["merchant_id", "is_active"]
    )
    op.create_unique_constraint(
        "uq_user_wallet_merchant_user",
        "user_wallets",
        ["merchant_id", "external_user_id"],
    )

    # Create wallet_addresses table
    op.create_table(
        "wallet_addresses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("derivation_index", sa.Integer(), nullable=False),
        sa.Column("encrypted_private_key", sa.String(512), nullable=False),
        sa.Column("is_active", sa.Boolean(), default=True, nullable=False),
        sa.Column("last_scanned_block", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_wallet_addresses_user_wallet_id", "wallet_addresses", ["user_wallet_id"]
    )
    op.create_index("ix_wallet_addresses_chain", "wallet_addresses", ["chain"])
    op.create_index("ix_wallet_addresses_address", "wallet_addresses", ["address"])
    op.create_index(
        "ix_wallet_address_chain_active", "wallet_addresses", ["chain", "is_active"]
    )
    op.create_unique_constraint(
        "uq_wallet_address_chain", "wallet_addresses", ["user_wallet_id", "chain"]
    )
    op.create_unique_constraint(
        "uq_chain_address", "wallet_addresses", ["chain", "address"]
    )

    # Create deposits table
    op.create_table(
        "deposits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wallet_address_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallet_addresses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("tx_hash", sa.String(66), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(36, 18), nullable=False),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("token_contract", sa.String(42), nullable=False),
        sa.Column("from_address", sa.String(42), nullable=False),
        sa.Column("status", deposit_status, default="pending", nullable=False),
        sa.Column("confirmations", sa.Integer(), default=0, nullable=False),
        sa.Column("required_confirmations", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sweep_tx_hash", sa.String(66), nullable=True),
        sa.Column("swept_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index("ix_deposits_user_wallet_id", "deposits", ["user_wallet_id"])
    op.create_index("ix_deposits_wallet_address_id", "deposits", ["wallet_address_id"])
    op.create_index("ix_deposits_chain", "deposits", ["chain"])
    op.create_index("ix_deposits_tx_hash", "deposits", ["tx_hash"])
    op.create_index("ix_deposits_status", "deposits", ["status"])
    op.create_index("ix_deposit_status_chain", "deposits", ["status", "chain"])
    op.create_unique_constraint(
        "uq_deposit_tx", "deposits", ["chain", "tx_hash", "log_index"]
    )

    # Create user_balances table
    op.create_table(
        "user_balances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("balance", sa.Numeric(36, 18), default=0, nullable=False),
        sa.Column("total_deposited", sa.Numeric(36, 18), default=0, nullable=False),
        sa.Column("total_withdrawn", sa.Numeric(36, 18), default=0, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index(
        "ix_user_balances_user_wallet_id", "user_balances", ["user_wallet_id"]
    )
    op.create_unique_constraint(
        "uq_user_balance_asset", "user_balances", ["user_wallet_id", "asset"]
    )


def downgrade() -> None:
    op.drop_table("user_balances")
    op.drop_table("deposits")
    op.drop_table("wallet_addresses")
    op.drop_table("user_wallets")

    # Drop enum
    op.execute("DROP TYPE IF EXISTS deposit_status")
