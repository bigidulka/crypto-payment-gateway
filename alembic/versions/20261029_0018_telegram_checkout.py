"""Merchant checkout profile and append-only credit acknowledgements for bot connectors."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_telegram_checkout"
down_revision = "0017_deposit_index_per_group"
branch_labels = None
depends_on = None


def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "checkout_profiles",
        sa.Column("merchant_id", uuid, sa.ForeignKey("merchants.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("profile", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("jsonb_typeof(profile)='object'", name="ck_checkout_profile_object"),
    )
    op.create_table(
        "checkout_payment_acks",
        sa.Column("invoice_id", uuid, sa.ForeignKey("invoices.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("merchant_id", uuid, sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_checkout_payment_acks_merchant", "checkout_payment_acks", ["merchant_id"])
    op.execute("""CREATE FUNCTION public.checkout_payment_ack_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'checkout payment acknowledgements are immutable'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.invoices i WHERE i.id=NEW.invoice_id
      AND i.merchant_id=NEW.merchant_id AND i.status='CONFIRMED') THEN
    RAISE EXCEPTION 'checkout acknowledgement requires a confirmed invoice of the merchant';
  END IF;
  RETURN NEW;
END $$""")
    op.execute(
        "CREATE TRIGGER checkout_payment_ack_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON checkout_payment_acks FOR EACH ROW EXECUTE FUNCTION public.checkout_payment_ack_guard()"
    )
    op.execute(
        "CREATE TRIGGER checkout_payment_acks_no_truncate BEFORE TRUNCATE "
        "ON checkout_payment_acks FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()"
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM checkout_payment_acks)")).scalar_one():
        raise RuntimeError("refusing to discard credit acknowledgements; bots would credit again")
    op.drop_table("checkout_payment_acks")
    op.execute("DROP FUNCTION public.checkout_payment_ack_guard()")
    op.drop_table("checkout_profiles")
