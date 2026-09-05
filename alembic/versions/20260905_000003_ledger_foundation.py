"""Add immutable balanced multi-tenant ledger foundation.

Revision ID: 0010_ledger_foundation
Revises: 0009_merchant_rails

This migration intentionally freezes its numeric bound rather than importing
runtime application code: migration history must stay reproducible if runtime
constants later change.
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_ledger_foundation"
down_revision = "0009_merchant_rails"

_ATOMIC_AMOUNT_UPPER_BOUND = 10**78
_ATOMIC_AMOUNT_UPPER_BOUND_SQL = str(_ATOMIC_AMOUNT_UPPER_BOUND)
_ATOMIC_AMOUNT_CHECK_SQL = (
    "CAST(amount_atomic AS TEXT) <> 'NaN' "
    "AND amount_atomic > 0 "
    f"AND amount_atomic < {_ATOMIC_AMOUNT_UPPER_BOUND_SQL} "
    "AND amount_atomic = trunc(amount_atomic)"
)


def upgrade() -> None:
    op.create_table(
        "ledger_assets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("network_kind", sa.String(16), nullable=False),
        sa.Column("network_identifier", sa.String(128), nullable=False),
        sa.Column("canonical_identifier", sa.String(256), nullable=False),
        sa.Column("is_native", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("atomic_decimals", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "atomic_decimals >= 0 AND atomic_decimals <= 255", name="ck_ledger_asset_decimals"
        ),
        sa.UniqueConstraint(
            "network_kind",
            "network_identifier",
            "canonical_identifier",
            "is_native",
            name="uq_ledger_asset_canonical",
        ),
    )
    op.create_table(
        "ledger_accounts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("account_type", sa.String(64), nullable=False),
        sa.Column("custody_type", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "account_type IN ('gateway_treasury_pending','merchant_payable','merchant_custodial_receivable','suspense_unmatched','refund_liability','merchant_fee_revenue','provider_fee_expense','network_fee_expense','provider_expense')",
            name="ck_ledger_account_type",
        ),
        sa.CheckConstraint(
            "custody_type IN ('gateway_managed','merchant_custodial','merchant_liability','system')",
            name="ck_ledger_custody_type",
        ),
        sa.UniqueConstraint("merchant_id", "id", name="uq_ledger_account_tenant"),
    )
    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_namespace", sa.String(128), nullable=False),
        sa.Column("source_external_id", sa.String(512), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("posting_digest", sa.String(64), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('open','posted')", name="ck_ledger_transaction_status"),
        sa.CheckConstraint(
            "(status = 'posted') = (posted_at IS NOT NULL)", name="ck_ledger_transaction_posted_at"
        ),
        sa.CheckConstraint("source_digest ~ '^[0-9a-f]{64}$'", name="ck_ledger_source_digest"),
        sa.CheckConstraint("posting_digest ~ '^[0-9a-f]{64}$'", name="ck_ledger_posting_digest"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("merchant_id", "id", name="uq_ledger_transaction_tenant"),
        sa.UniqueConstraint(
            "merchant_id", "idempotency_key", name="uq_ledger_transaction_idempotency"
        ),
        sa.UniqueConstraint(
            "source_namespace", "source_external_id", name="uq_ledger_transaction_source"
        ),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("ledger_transaction_id", sa.UUID(), nullable=False),
        sa.Column("ledger_account_id", sa.UUID(), nullable=False),
        sa.Column("ledger_asset_id", sa.UUID(), nullable=False),
        sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount_atomic", sa.Numeric(), nullable=False),
        sa.CheckConstraint("direction IN ('debit','credit')", name="ck_ledger_entry_direction"),
        sa.CheckConstraint(_ATOMIC_AMOUNT_CHECK_SQL, name="ck_ledger_entry_atomic"),
        sa.ForeignKeyConstraint(
            ["merchant_id", "ledger_transaction_id"],
            ["ledger_transactions.merchant_id", "ledger_transactions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "ledger_account_id"],
            ["ledger_accounts.merchant_id", "ledger_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
    )
    op.execute("""CREATE FUNCTION public.ledger_assert_final_state() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE tid uuid; invalid boolean;
BEGIN
  IF TG_TABLE_NAME = 'ledger_transactions' THEN
    IF TG_OP = 'INSERT' THEN tid := NEW.id; ELSE tid := OLD.id; END IF;
  ELSIF TG_OP = 'INSERT' THEN tid := NEW.ledger_transaction_id;
  ELSE tid := OLD.ledger_transaction_id;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.ledger_transactions WHERE id=tid) THEN RETURN NULL; END IF;
  IF EXISTS (SELECT 1 FROM public.ledger_transactions WHERE id=tid AND status <> 'posted') THEN
    RAISE EXCEPTION 'ledger transaction must be posted before commit';
  END IF;
  SELECT EXISTS(
    SELECT 1 FROM (
      SELECT ledger_asset_id, count(*) AS line_count,
        sum(CASE direction WHEN 'debit' THEN amount_atomic ELSE -amount_atomic END) AS signed_total
      FROM public.ledger_entries WHERE ledger_transaction_id=tid GROUP BY ledger_asset_id
    ) balances WHERE balances.line_count < 2 OR balances.signed_total <> 0
  ) INTO invalid;
  IF invalid OR NOT EXISTS (SELECT 1 FROM public.ledger_entries WHERE ledger_transaction_id=tid) THEN
    RAISE EXCEPTION 'posted ledger transaction must have balanced entries';
  END IF;
  RETURN NULL;
END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER ledger_final_header AFTER INSERT OR UPDATE ON ledger_transactions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.ledger_assert_final_state()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER ledger_final_entry AFTER INSERT OR UPDATE OR DELETE ON ledger_entries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.ledger_assert_final_state()"
    )
    op.execute("""CREATE FUNCTION public.ledger_guard_row() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE current_status text;
BEGIN
  IF TG_TABLE_NAME IN ('ledger_assets', 'ledger_accounts') THEN
    RAISE EXCEPTION 'ledger identity data is immutable';
  END IF;
  IF TG_TABLE_NAME = 'ledger_transactions' THEN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'ledger transaction deletion is forbidden'; END IF;
    IF TG_OP = 'UPDATE' AND (
      OLD.status <> 'open' OR NEW.status <> 'posted' OR NEW.posted_at IS NULL OR
      NEW.id <> OLD.id OR NEW.merchant_id <> OLD.merchant_id OR
      NEW.source_namespace <> OLD.source_namespace OR NEW.source_external_id <> OLD.source_external_id OR
      NEW.source_digest <> OLD.source_digest OR NEW.idempotency_key <> OLD.idempotency_key OR
      NEW.posting_digest <> OLD.posting_digest OR NEW.created_at <> OLD.created_at
    ) THEN RAISE EXCEPTION 'ledger transactions are immutable'; END IF;
    RETURN NEW;
  END IF;
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'ledger entries are immutable'; END IF;
  SELECT status INTO current_status FROM public.ledger_transactions WHERE id=NEW.ledger_transaction_id FOR UPDATE;
  IF current_status IS NULL OR current_status <> 'open' THEN RAISE EXCEPTION 'posted ledger data is immutable'; END IF;
  RETURN NEW;
END $$""")
    op.execute(
        "CREATE TRIGGER ledger_assets_guard BEFORE UPDATE OR DELETE ON ledger_assets FOR EACH ROW EXECUTE FUNCTION public.ledger_guard_row()"
    )
    op.execute(
        "CREATE TRIGGER ledger_accounts_guard BEFORE UPDATE OR DELETE ON ledger_accounts FOR EACH ROW EXECUTE FUNCTION public.ledger_guard_row()"
    )
    op.execute(
        "CREATE TRIGGER ledger_transactions_guard BEFORE UPDATE OR DELETE ON ledger_transactions FOR EACH ROW EXECUTE FUNCTION public.ledger_guard_row()"
    )
    op.execute(
        "CREATE TRIGGER ledger_entries_guard BEFORE INSERT OR UPDATE OR DELETE ON ledger_entries FOR EACH ROW EXECUTE FUNCTION public.ledger_guard_row()"
    )
    op.execute("""CREATE FUNCTION public.ledger_prevent_truncate() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN RAISE EXCEPTION 'ledger table truncation is forbidden'; END $$""")
    for table in ("ledger_assets", "ledger_accounts", "ledger_transactions", "ledger_entries"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()"
        )


def downgrade() -> None:
    posted_rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM public.ledger_transactions WHERE status='posted')"
            )
        )
        .scalar()
    )
    if posted_rows:
        raise RuntimeError("refusing destructive ledger downgrade while posted journal rows exist")
    op.execute("DROP FUNCTION IF EXISTS public.ledger_prevent_truncate() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS public.ledger_guard_row() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS public.ledger_assert_final_state() CASCADE")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("ledger_accounts")
    op.drop_table("ledger_assets")
