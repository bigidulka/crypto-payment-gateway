"""Serialize settlement facts with immutable receipt proof, additive to 0013."""

from alembic import op

revision = "0014_settlement_receipt_binding"
down_revision = "0013_gateway_payouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION public.gateway_settlement_receipt_lock() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  -- Both insert paths acquire the same row lock before reading each other's
  -- facts. A concurrent receipt cannot prove gross while settlement proves net.
  PERFORM 1 FROM public.provider_payment_observations
    WHERE id=NEW.observation_id AND merchant_id=NEW.merchant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'settlement or receipt observation missing'; END IF;
  IF TG_TABLE_NAME='provider_settlements' AND EXISTS (
      SELECT 1 FROM public.provider_payment_receipts
      WHERE observation_id=NEW.observation_id AND merchant_id=NEW.merchant_id
  ) THEN
    RAISE EXCEPTION 'settlement cannot change an already proven receipt';
  END IF;
  RETURN NEW;
END $$""")
    for table in ("provider_settlements", "provider_payment_receipts"):
        # PostgreSQL orders same-event triggers by name. This lock executes
        # before each existing proof guard, including the legacy receipt guard.
        op.execute(f"""CREATE TRIGGER gateway_receipt_serialization BEFORE INSERT ON {table}
FOR EACH ROW EXECUTE FUNCTION public.gateway_settlement_receipt_lock()""")


def downgrade() -> None:
    for table in ("provider_settlements", "provider_payment_receipts"):
        op.execute(f"DROP TRIGGER gateway_receipt_serialization ON {table}")
    op.execute("DROP FUNCTION public.gateway_settlement_receipt_lock()")
