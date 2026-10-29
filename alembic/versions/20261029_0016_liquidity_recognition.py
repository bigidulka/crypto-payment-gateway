"""Prove pre-gateway provider funds once before a sole-owner payout may spend them."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_liquidity_recognition"
down_revision = "0015_onchain_operations"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    atomic = "{0} >= 0 AND {0} < 1e78 AND {0} = trunc({0})"
    op.create_table(
        "gateway_liquidity_recognitions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("merchant_id", uuid, nullable=False),
        sa.Column("rail_id", uuid, nullable=False),
        sa.Column("ledger_asset_id", uuid, nullable=False),
        sa.Column("amount_atomic", sa.Numeric(), nullable=False),
        sa.Column("provider_available_atomic", sa.Numeric(), nullable=False),
        sa.Column("provider_reserve_atomic", sa.Numeric(), nullable=False),
        sa.Column("ledger_outstanding_atomic", sa.Numeric(), nullable=False),
        sa.Column("open_invoice_atomic", sa.Numeric(), nullable=False),
        sa.Column("payout_operation_id", uuid, nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("ledger_transaction_id", uuid, nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["rail_id"], ["gateway_rail_connections.rail_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payout_operation_id"], ["gateway_operations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["merchant_id", "ledger_transaction_id"],
            ["ledger_transactions.merchant_id", "ledger_transactions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("ledger_transaction_id", name="uq_liquidity_recognition_journal"),
        sa.UniqueConstraint("payout_operation_id", name="uq_liquidity_recognition_payout"),
        sa.CheckConstraint(
            "amount_atomic > 0 AND " + " AND ".join(
                atomic.format(column)
                for column in (
                    "amount_atomic",
                    "provider_available_atomic",
                    "provider_reserve_atomic",
                    "ledger_outstanding_atomic",
                    "open_invoice_atomic",
                )
            ),
            name="ck_liquidity_recognition_amounts",
        ),
        # Only physical funds that no ledger obligation, reserve or open invoice explains.
        sa.CheckConstraint(
            "amount_atomic <= provider_available_atomic - provider_reserve_atomic"
            " - ledger_outstanding_atomic - open_invoice_atomic",
            name="ck_liquidity_recognition_surplus",
        ),
        sa.CheckConstraint("length(evidence_digest)=64", name="ck_liquidity_recognition_digest"),
    )
    op.execute("""CREATE FUNCTION public.gateway_liquidity_recognition_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'liquidity recognitions are immutable'; END IF;
  IF NOT EXISTS(
    SELECT 1 FROM public.gateway_rail_connections c
    JOIN public.gateway_operations o ON o.id=NEW.payout_operation_id AND o.merchant_id=c.merchant_id
      AND o.rail_id=c.rail_id AND o.operation_type='payout' AND o.ledger_asset_id=NEW.ledger_asset_id
      AND o.amount_atomic>=NEW.amount_atomic AND o.status='submitting'
    JOIN public.ledger_transactions t ON t.id=NEW.ledger_transaction_id AND t.merchant_id=c.merchant_id
    WHERE c.rail_id=NEW.rail_id AND c.merchant_id=NEW.merchant_id
      AND t.status='posted' AND t.source_namespace='gateway_liquidity_recognition'
      AND t.source_external_id=NEW.id::text AND t.source_digest=NEW.evidence_digest
      AND (SELECT count(*) FROM public.ledger_entries e WHERE e.ledger_transaction_id=t.id)=2
      AND EXISTS(SELECT 1 FROM public.ledger_entries e WHERE e.ledger_transaction_id=t.id AND e.merchant_id=c.merchant_id AND e.ledger_asset_id=NEW.ledger_asset_id AND e.ledger_account_id=c.receivable_account_id AND e.direction='debit' AND e.amount_atomic=NEW.amount_atomic)
      AND EXISTS(SELECT 1 FROM public.ledger_entries e WHERE e.ledger_transaction_id=t.id AND e.merchant_id=c.merchant_id AND e.ledger_asset_id=NEW.ledger_asset_id AND e.ledger_account_id=c.payable_account_id AND e.direction='credit' AND e.amount_atomic=NEW.amount_atomic)
  ) THEN RAISE EXCEPTION 'liquidity recognition lacks exact tenant journal and payout proof'; END IF;
  RETURN NEW;
END $$""")
    op.execute(
        "CREATE TRIGGER gateway_liquidity_recognition_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON gateway_liquidity_recognitions FOR EACH ROW EXECUTE FUNCTION public.gateway_liquidity_recognition_guard()"
    )
    op.execute(
        "CREATE TRIGGER gateway_liquidity_recognitions_no_truncate BEFORE TRUNCATE "
        "ON gateway_liquidity_recognitions FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()"
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM gateway_liquidity_recognitions)")).scalar_one():
        raise RuntimeError("refusing to discard recognized liquidity; disable feature and roll back code")
    op.drop_table("gateway_liquidity_recognitions")
    op.execute("DROP FUNCTION public.gateway_liquidity_recognition_guard()")
