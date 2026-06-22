"""Add per-check address lease lifecycle.

Revision ID: 0007_per_check_address_leases
Revises: 0006_migrate_sweep_data
Create Date: 2026-06-22

Adds lease lifecycle fields to existing invoice payment tables:
- deposit_addresses becomes reusable address pool with lease/cooldown statuses
- payment_sessions becomes the per-check lease record
- address_lease_events records lifecycle audit events
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_per_check_address_leases"
down_revision = "0006_migrate_sweep_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    lease_status = postgresql.ENUM(
        "available",
        "leased",
        "cooldown",
        "retired",
        name="deposit_address_lease_status",
        create_type=False,
    )
    lease_status.create(op.get_bind(), checkfirst=True)

    session_status = postgresql.ENUM(
        "pending",
        "seen_onchain",
        "paid",
        "expired",
        "late",
        "cancelled",
        name="payment_session_status",
        create_type=False,
    )
    session_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "deposit_addresses",
        sa.Column(
            "lease_status",
            lease_status,
            nullable=False,
            server_default="available",
        ),
    )
    op.add_column(
        "deposit_addresses",
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deposit_addresses",
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deposit_addresses",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deposit_addresses",
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "payment_sessions",
        sa.Column(
            "status",
            session_status,
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "payment_sessions",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payment_sessions",
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payment_sessions",
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "address_lease_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deposit_address_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("previous_status", sa.String(30), nullable=True),
        sa.Column("new_status", sa.String(30), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["deposit_address_id"],
            ["deposit_addresses.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["payment_session_id"],
            ["payment_sessions.id"],
            ondelete="SET NULL",
        ),
    )

    op.create_index(
        "idx_deposit_addr_lease",
        "deposit_addresses",
        ["chain_group", "lease_status"],
    )
    op.create_index(
        "idx_deposit_addr_lease_until",
        "deposit_addresses",
        ["leased_until"],
    )
    op.create_index(
        "idx_deposit_addr_cooldown_until",
        "deposit_addresses",
        ["cooldown_until"],
    )
    op.create_index(
        "idx_session_status_expires",
        "payment_sessions",
        ["status", "expires_at"],
    )
    op.create_index(
        "uq_payment_session_address_active",
        "payment_sessions",
        ["deposit_address_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'seen_onchain')"),
    )
    op.create_index(
        "idx_address_lease_events_address_created",
        "address_lease_events",
        ["deposit_address_id", "created_at"],
    )
    op.create_index(
        "idx_address_lease_events_session_created",
        "address_lease_events",
        ["payment_session_id", "created_at"],
    )

    op.execute(
        """
        UPDATE payment_sessions AS ps
        SET expires_at = i.expires_at,
            status = CASE
                WHEN i.status = 'SEEN_ONCHAIN' THEN 'seen_onchain'::payment_session_status
                WHEN i.status = 'CONFIRMED' THEN 'paid'::payment_session_status
                WHEN i.status = 'EXPIRED' THEN 'expired'::payment_session_status
                ELSE 'pending'::payment_session_status
            END,
            paid_at = CASE WHEN i.status = 'CONFIRMED' THEN now() ELSE NULL END
        FROM invoices AS i
        WHERE ps.invoice_id = i.id
        """
    )
    op.execute(
        """
        UPDATE deposit_addresses AS da
        SET lease_status = 'leased'::deposit_address_lease_status,
            is_used = true,
            leased_until = ps.expires_at,
            cooldown_until = NULL,
            last_used_at = ps.chosen_at
        FROM payment_sessions AS ps
        WHERE da.id = ps.deposit_address_id
          AND ps.status IN ('pending', 'seen_onchain')
        """
    )
    op.execute(
        """
        WITH active_sessions AS (
            SELECT deposit_address_id
            FROM payment_sessions
            WHERE status IN ('pending', 'seen_onchain')
        ),
        latest_terminal AS (
            SELECT DISTINCT ON (ps.deposit_address_id)
                ps.deposit_address_id,
                ps.chosen_at,
                ps.expires_at + make_interval(mins => i.ttl_minutes) AS cooldown_until
            FROM payment_sessions AS ps
            JOIN invoices AS i ON i.id = ps.invoice_id
            WHERE ps.status IN ('paid', 'expired', 'late', 'cancelled')
            ORDER BY ps.deposit_address_id, ps.chosen_at DESC
        )
        UPDATE deposit_addresses AS da
        SET lease_status = CASE
                WHEN lt.cooldown_until > now()
                    THEN 'cooldown'::deposit_address_lease_status
                ELSE 'available'::deposit_address_lease_status
            END,
            is_used = CASE WHEN lt.cooldown_until > now() THEN true ELSE false END,
            leased_until = NULL,
            cooldown_until = CASE
                WHEN lt.cooldown_until > now() THEN lt.cooldown_until
                ELSE NULL
            END,
            last_used_at = lt.chosen_at
        FROM latest_terminal AS lt
        WHERE da.id = lt.deposit_address_id
          AND NOT EXISTS (
              SELECT 1
              FROM active_sessions AS aps
              WHERE aps.deposit_address_id = da.id
          )
        """
    )
    op.execute(
        """
        UPDATE deposit_addresses AS da
        SET lease_status = 'available'::deposit_address_lease_status,
            is_used = false,
            leased_until = NULL,
            cooldown_until = NULL
        WHERE NOT EXISTS (
            SELECT 1
            FROM payment_sessions AS ps
            WHERE ps.deposit_address_id = da.id
        )
          AND da.retired_at IS NULL
        """
    )

    op.alter_column("deposit_addresses", "lease_status", server_default=None)
    op.alter_column("payment_sessions", "status", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_address_lease_events_session_created", table_name="address_lease_events")
    op.drop_index("idx_address_lease_events_address_created", table_name="address_lease_events")
    op.drop_index("uq_payment_session_address_active", table_name="payment_sessions")
    op.drop_index("idx_session_status_expires", table_name="payment_sessions")
    op.drop_index("idx_deposit_addr_cooldown_until", table_name="deposit_addresses")
    op.drop_index("idx_deposit_addr_lease_until", table_name="deposit_addresses")
    op.drop_index("idx_deposit_addr_lease", table_name="deposit_addresses")
    op.drop_table("address_lease_events")

    op.drop_column("payment_sessions", "released_at")
    op.drop_column("payment_sessions", "paid_at")
    op.drop_column("payment_sessions", "expires_at")
    op.drop_column("payment_sessions", "status")

    op.drop_column("deposit_addresses", "retired_at")
    op.drop_column("deposit_addresses", "last_used_at")
    op.drop_column("deposit_addresses", "cooldown_until")
    op.drop_column("deposit_addresses", "leased_until")
    op.drop_column("deposit_addresses", "lease_status")

    op.execute("DROP TYPE IF EXISTS payment_session_status")
    op.execute("DROP TYPE IF EXISTS deposit_address_lease_status")
