"""Initial migration - create all tables

Revision ID: 0001
Revises:
Create Date: 2025-12-25

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20251225_000000_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # === ENUMS ===
    # Создаём enum типы напрямую через execute
    op.execute("CREATE TYPE invoice_status AS ENUM ('CREATED', 'AWAITING_PAYMENT', 'SEEN_ONCHAIN', 'CONFIRMED', 'EXPIRED')")
    op.execute("CREATE TYPE tx_status AS ENUM ('pending', 'confirming', 'confirmed')")
    op.execute("CREATE TYPE sweep_state AS ENUM ('pending_gas', 'funding', 'sweeping', 'completed', 'failed')")
    op.execute("CREATE TYPE outbox_status AS ENUM ('pending', 'sent', 'failed')")

    # Создаём объекты для использования в столбцах (без автосоздания типа)
    invoice_status_enum = postgresql.ENUM(
        "CREATED",
        "AWAITING_PAYMENT",
        "SEEN_ONCHAIN",
        "CONFIRMED",
        "EXPIRED",
        name="invoice_status",
        create_type=False,
    )

    tx_status_enum = postgresql.ENUM(
        "pending", "confirming", "confirmed", name="tx_status", create_type=False
    )

    sweep_state_enum = postgresql.ENUM(
        "pending_gas", "funding", "sweeping", "completed", "failed", name="sweep_state", create_type=False
    )

    outbox_status_enum = postgresql.ENUM(
        "pending", "sent", "failed", name="outbox_status", create_type=False
    )

    # === MERCHANTS ===
    op.create_table(
        "merchants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_merchants_email", "merchants", ["email"])

    # === API_KEYS ===
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False, default="Default"),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_api_keys_merchant", "api_keys", ["merchant_id"])

    # === WEBHOOKS ===
    op.create_table(
        "webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("secret", sa.String(64), nullable=False),
        sa.Column(
            "events", postgresql.ARRAY(sa.String(50)), nullable=False, default=[]
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, default=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_webhooks_merchant_active", "webhooks", ["merchant_id", "is_active"]
    )

    # === INVOICES ===
    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_id", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(36, 18), nullable=False),
        sa.Column("asset", sa.String(10), nullable=False),
        sa.Column("allowed_chains", postgresql.ARRAY(sa.String(20)), nullable=False),
        sa.Column("status", invoice_status_enum, nullable=False, default="CREATED"),
        sa.Column("ttl_minutes", sa.Integer(), nullable=False, default=60),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra_data", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_invoices_public_id", "invoices", ["public_id"])
    op.create_index("idx_invoices_status_expires", "invoices", ["status", "expires_at"])
    op.create_index(
        "idx_invoices_merchant_created", "invoices", ["merchant_id", "created_at"]
    )
    op.create_unique_constraint(
        "uq_invoice_idempotency", "invoices", ["merchant_id", "idempotency_key"]
    )

    # === DEPOSIT_ADDRESSES ===
    op.create_table(
        "deposit_addresses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("address", sa.String(42), nullable=False, unique=True),
        sa.Column("encrypted_privkey", postgresql.BYTEA(), nullable=False),
        sa.Column("chain_group", sa.String(20), nullable=False, default="evm"),
        sa.Column("derivation_path", sa.String(50), nullable=False),
        sa.Column("derivation_index", sa.Integer(), nullable=False, unique=True),
        sa.Column("is_used", sa.Boolean(), nullable=False, default=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_deposit_addr_available", "deposit_addresses", ["chain_group", "is_used"]
    )

    # === PAYMENT_SESSIONS ===
    op.create_table(
        "payment_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chain", sa.String(20), nullable=False),
        sa.Column("token", sa.String(10), nullable=False),
        sa.Column(
            "deposit_address_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deposit_addresses.id"),
            nullable=False,
        ),
        sa.Column(
            "chosen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_session_address", "payment_sessions", ["deposit_address_id"])
    op.create_unique_constraint(
        "uq_session_invoice_chain_token",
        "payment_sessions",
        ["invoice_id", "chain", "token"],
    )

    # === ONCHAIN_TXS ===
    op.create_table(
        "onchain_txs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chain", sa.String(20), nullable=False),
        sa.Column("tx_hash", sa.String(66), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("from_address", sa.String(42), nullable=False),
        sa.Column("to_address", sa.String(42), nullable=False),
        sa.Column("token_contract", sa.String(42), nullable=False),
        sa.Column("amount", sa.Numeric(36, 18), nullable=False),
        sa.Column(
            "payment_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", tx_status_enum, nullable=False, default="pending"),
        sa.Column("confirmations", sa.Integer(), nullable=False, default=0),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_onchain_to_address", "onchain_txs", ["to_address"])
    op.create_index("idx_onchain_status", "onchain_txs", ["status"])
    op.create_unique_constraint(
        "uq_onchain_chain_tx_log", "onchain_txs", ["chain", "tx_hash", "log_index"]
    )

    # === INVOICE_EVENTS ===
    op.create_table(
        "invoice_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_events_invoice_created", "invoice_events", ["invoice_id", "created_at"]
    )

    # === SWEEP_JOBS ===
    op.create_table(
        "sweep_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "payment_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("payment_sessions.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("state", sweep_state_enum, nullable=False, default="pending_gas"),
        sa.Column("gas_tx_hash", sa.String(66), nullable=True),
        sa.Column("sweep_tx_hash", sa.String(66), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, default=0),
        sa.Column("max_attempts", sa.Integer(), nullable=False, default=10),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("idx_sweep_state_retry", "sweep_jobs", ["state", "next_retry_at"])

    # === OUTBOX_WEBHOOKS ===
    op.create_table(
        "outbox_webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "webhook_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invoice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, default=0),
        sa.Column("max_attempts", sa.Integer(), nullable=False, default=5),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("status", outbox_status_enum, nullable=False, default="pending"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_outbox_pending_retry", "outbox_webhooks", ["status", "next_retry_at"]
    )

    # === CHAIN_CHECKPOINTS ===
    op.create_table(
        "chain_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chain", sa.String(20), nullable=False, unique=True),
        sa.Column("last_scanned_block", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # Удаляем таблицы в обратном порядке
    op.drop_table("chain_checkpoints")
    op.drop_table("outbox_webhooks")
    op.drop_table("sweep_jobs")
    op.drop_table("invoice_events")
    op.drop_table("onchain_txs")
    op.drop_table("payment_sessions")
    op.drop_table("deposit_addresses")
    op.drop_table("invoices")
    op.drop_table("webhooks")
    op.drop_table("api_keys")
    op.drop_table("merchants")

    # Удаляем enums
    op.execute("DROP TYPE IF EXISTS outbox_status")
    op.execute("DROP TYPE IF EXISTS sweep_state")
    op.execute("DROP TYPE IF EXISTS tx_status")
    op.execute("DROP TYPE IF EXISTS invoice_status")
