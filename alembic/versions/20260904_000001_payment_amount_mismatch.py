"""Record what actually arrived when it does not match the invoice.

Revision ID: 0008_payment_amount_mismatch
Revises: 0007_per_check_address_leases
Create Date: 2026-09-04

The scanner used to drop any transfer whose amount or token did not match the
invoice exactly. The user saw an unpaid invoice with no explanation while the
funds sat on the leased address. These columns let the scanner record the
observed transfer so the status endpoint — and the bot — can say what happened.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_payment_amount_mismatch"
down_revision = "0007_per_check_address_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payment_sessions",
        sa.Column("received_amount", sa.Numeric(36, 18), nullable=True),
    )
    op.add_column(
        "payment_sessions",
        sa.Column("mismatch_reason", sa.String(32), nullable=True),
    )
    op.add_column(
        "payment_sessions",
        sa.Column("mismatch_token", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_sessions", "mismatch_token")
    op.drop_column("payment_sessions", "mismatch_reason")
    op.drop_column("payment_sessions", "received_amount")
