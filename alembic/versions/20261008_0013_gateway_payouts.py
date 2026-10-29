"""Exact payout receipts and durable reservation identity."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013_gateway_payouts"
down_revision = "0012_gateway_orchestration"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gateway_operations", sa.Column("destination_type", sa.String(32), nullable=False, server_default="telegram_user_id"))
    op.create_table("gateway_payout_receipts",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_account_identity", sa.String(64), nullable=False),
        sa.Column("provider_reference", sa.String(256), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("ledger_transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["operation_id"], ["gateway_operations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merchant_id", "ledger_transaction_id"], ["ledger_transactions.merchant_id", "ledger_transactions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("provider_account_identity", "provider_reference", name="uq_gateway_payout_provider_receipt"),
        sa.CheckConstraint("length(provider_account_identity)=64 AND length(evidence_digest)=64 AND length(provider_reference)>0", name="ck_gateway_payout_evidence"))
    op.execute("""CREATE FUNCTION public.gateway_payout_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'payout receipts are immutable'; END IF;
  IF NOT EXISTS(
    SELECT 1 FROM public.gateway_operations o
    JOIN public.gateway_rail_connections c ON c.rail_id=o.rail_id AND c.merchant_id=o.merchant_id
    JOIN public.ledger_transactions t ON t.id=NEW.ledger_transaction_id AND t.merchant_id=o.merchant_id
    WHERE o.id=NEW.operation_id AND o.merchant_id=NEW.merchant_id AND o.operation_type='payout'
      AND c.account_identity=NEW.provider_account_identity
      AND t.status='posted' AND t.source_namespace='gateway_provider_payout'
      AND t.source_external_id=o.id::text AND t.source_digest=o.request_digest
      AND (SELECT count(*) FROM public.ledger_entries e WHERE e.ledger_transaction_id=t.id)=2
      AND EXISTS(SELECT 1 FROM public.ledger_entries e WHERE e.ledger_transaction_id=t.id AND e.merchant_id=o.merchant_id AND e.ledger_asset_id=o.ledger_asset_id AND e.ledger_account_id=c.payable_account_id AND e.direction='debit' AND e.amount_atomic=o.amount_atomic)
      AND EXISTS(SELECT 1 FROM public.ledger_entries e WHERE e.ledger_transaction_id=t.id AND e.merchant_id=o.merchant_id AND e.ledger_asset_id=o.ledger_asset_id AND e.ledger_account_id=c.receivable_account_id AND e.direction='credit' AND e.amount_atomic=o.amount_atomic)
  ) THEN RAISE EXCEPTION 'payout receipt lacks exact tenant journal proof'; END IF;
  RETURN NEW;
END $$""")
    op.execute("CREATE TRIGGER gateway_payout_guard BEFORE INSERT OR UPDATE OR DELETE ON gateway_payout_receipts FOR EACH ROW EXECUTE FUNCTION public.gateway_payout_guard()")
    op.execute("CREATE TRIGGER gateway_payout_no_truncate BEFORE TRUNCATE ON gateway_payout_receipts FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()")
    op.execute("""CREATE FUNCTION public.gateway_payout_operation_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF TG_OP='UPDATE' AND NEW.destination_type IS DISTINCT FROM OLD.destination_type THEN
    RAISE EXCEPTION 'payout recipient type is immutable';
  END IF;
  IF NEW.operation_type='payout' AND NEW.status='completed' AND NOT EXISTS(
    SELECT 1 FROM public.gateway_payout_receipts pr WHERE pr.operation_id=NEW.id AND pr.merchant_id=NEW.merchant_id AND pr.provider_reference=NEW.provider_reference
  ) THEN RAISE EXCEPTION 'payout completion lacks receipt'; END IF;
  RETURN NEW;
END $$""")
    op.execute("CREATE TRIGGER gateway_payout_operation_guard BEFORE INSERT OR UPDATE ON gateway_operations FOR EACH ROW EXECUTE FUNCTION public.gateway_payout_operation_guard()")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM gateway_operations WHERE operation_type='payout')")).scalar_one():
        raise RuntimeError("refusing to discard durable payouts; disable feature and roll back code")
    op.execute("DROP TRIGGER gateway_payout_operation_guard ON gateway_operations")
    op.execute("DROP FUNCTION public.gateway_payout_operation_guard()")
    op.drop_table("gateway_payout_receipts")
    op.execute("DROP FUNCTION public.gateway_payout_guard()")
    op.drop_column("gateway_operations", "destination_type")
