"""Durable gateway orchestration and exact net settlement, additive to 0011."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_gateway_orchestration"
down_revision = "0011_provider_payment_ingestion"
branch_labels = None
depends_on = None

BASE_GUARD = "CREATE FUNCTION public.provider_payment_ingestion_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$\nDECLARE valid boolean; locked_event public.invoice_events%ROWTYPE;\nBEGIN\n  IF TG_OP <> 'INSERT' THEN\n    RAISE EXCEPTION 'provider payment ingestion source data is immutable';\n  END IF;\n  IF TG_TABLE_NAME = 'provider_payment_mappings' THEN\n    SELECT EXISTS(\n      SELECT 1\n      FROM public.invoices i\n      JOIN public.rails r ON r.id=NEW.rail_id AND r.merchant_id=NEW.merchant_id\n      JOIN public.ledger_assets a ON a.id=NEW.ledger_asset_id\n      JOIN public.ledger_accounts receivable ON receivable.id=NEW.receivable_account_id AND receivable.merchant_id=NEW.merchant_id\n      JOIN public.ledger_accounts payable ON payable.id=NEW.payable_account_id AND payable.merchant_id=NEW.merchant_id\n      WHERE i.id=NEW.invoice_id AND i.merchant_id=NEW.merchant_id\n        AND i.status IN ('CREATED','AWAITING_PAYMENT')\n        AND i.asset=NEW.provider_asset_code\n        AND i.amount * power(10::numeric, a.atomic_decimals) = NEW.expected_amount_atomic\n        AND r.rail_type='cryptobot' AND r.is_active AND r.network=NEW.provider_network\n        AND r.assets::jsonb @> jsonb_build_array(NEW.provider_asset_code)\n        AND a.network_kind='custodial' AND a.network_identifier='cryptobot:' || NEW.provider_network\n        AND a.canonical_identifier=NEW.provider_asset_code AND NOT a.is_native\n        AND receivable.account_type='merchant_custodial_receivable' AND receivable.custody_type='merchant_custodial'\n        AND payable.account_type='merchant_payable' AND payable.custody_type='merchant_liability'\n    ) INTO valid;\n    IF NOT valid THEN RAISE EXCEPTION 'provider mapping does not bind exact tenant canonical asset amount rail and accounts'; END IF;\n  ELSIF TG_TABLE_NAME = 'provider_payment_observations' THEN\n    SELECT EXISTS(\n      SELECT 1 FROM public.provider_payment_mappings m\n      WHERE m.id=NEW.mapping_id AND m.merchant_id=NEW.merchant_id\n        AND m.rail_id=NEW.rail_id\n        AND m.provider_account_identity=NEW.provider_account_identity\n        AND m.provider_network=NEW.provider_network\n        AND m.provider_invoice_id=NEW.provider_invoice_id\n        AND m.ledger_asset_id=NEW.ledger_asset_id\n    ) INTO valid;\n    IF NOT valid THEN RAISE EXCEPTION 'provider observation does not match its mapping'; END IF;\n  ELSIF TG_TABLE_NAME = 'provider_payment_receipts' THEN\n    SELECT * INTO locked_event FROM public.invoice_events WHERE id=NEW.invoice_event_id FOR UPDATE;\n    IF NOT FOUND THEN RAISE EXCEPTION 'provider receipt event missing'; END IF;\n    SELECT EXISTS(\n      SELECT 1\n      FROM public.provider_payment_observations o\n      JOIN public.provider_verification_decisions d ON d.id=NEW.decision_id AND d.merchant_id=NEW.merchant_id AND d.observation_id=NEW.observation_id AND d.decision='verified'\n      JOIN public.provider_payment_mappings m ON m.id=o.mapping_id AND m.merchant_id=NEW.merchant_id\n      JOIN public.ledger_assets a ON a.id=o.ledger_asset_id\n      JOIN public.ledger_transactions t ON t.id=NEW.ledger_transaction_id AND t.merchant_id=NEW.merchant_id AND t.status='posted' AND t.source_namespace='provider_payment_observation' AND t.source_external_id=o.economic_digest AND t.source_digest=o.economic_digest\n      JOIN public.invoices i ON i.id=NEW.invoice_id AND i.id=m.invoice_id AND i.merchant_id=NEW.merchant_id AND i.status='CONFIRMED'\n      WHERE o.id=NEW.observation_id AND o.merchant_id=NEW.merchant_id\n        AND locked_event.invoice_id=NEW.invoice_id AND locked_event.event_id=NEW.event_id AND locked_event.event_type='invoice.confirmed'\n        AND NEW.event_id=o.economic_digest\n        AND m.expected_amount_atomic=o.observed_amount_atomic\n        AND m.receivable_account_id <> m.payable_account_id\n        AND (SELECT count(*) FROM public.ledger_entries le WHERE le.ledger_transaction_id=t.id) = 2\n        AND EXISTS(SELECT 1 FROM public.ledger_entries le WHERE le.ledger_transaction_id=t.id AND le.merchant_id=NEW.merchant_id AND le.ledger_account_id=m.receivable_account_id AND le.ledger_asset_id=o.ledger_asset_id AND le.direction='debit' AND le.amount_atomic=o.observed_amount_atomic)\n        AND EXISTS(SELECT 1 FROM public.ledger_entries le WHERE le.ledger_transaction_id=t.id AND le.merchant_id=NEW.merchant_id AND le.ledger_account_id=m.payable_account_id AND le.ledger_asset_id=o.ledger_asset_id AND le.direction='credit' AND le.amount_atomic=o.observed_amount_atomic)\n        AND locked_event.payload::jsonb=jsonb_build_object(\n          'event','invoice.confirmed',\n          'event_id',NEW.event_id,\n          'invoice',jsonb_build_object('id',NEW.invoice_id::text,'public_id',i.public_id,'status','CONFIRMED'),\n          'observed',jsonb_build_object(\n            'ledger_asset_id',o.ledger_asset_id::text,\n            'network_kind',a.network_kind,\n            'network_identifier',a.network_identifier,\n            'atomic_decimals',a.atomic_decimals,\n            'provider_account_identity',o.provider_account_identity,\n            'provider_network',o.provider_network,\n            'asset_code',m.provider_asset_code,\n            'amount_atomic',o.observed_amount_atomic::text\n          )\n        )\n    ) INTO valid;\n    IF NOT valid THEN RAISE EXCEPTION 'provider receipt lacks exact verified journal event and amount projection'; END IF;\n  ELSIF TG_TABLE_NAME = 'provider_payment_recipient_intents' THEN\n    SELECT EXISTS(\n      SELECT 1\n      FROM public.provider_payment_receipts r\n      JOIN public.invoice_events e ON e.id=r.invoice_event_id AND e.event_id=r.event_id\n      JOIN public.outbox_webhooks ob ON ob.id=NEW.outbox_id AND ob.invoice_id=r.invoice_id AND ob.webhook_id=NEW.webhook_id AND ob.event_type='invoice.confirmed'\n      JOIN public.webhooks w ON w.id=NEW.webhook_id AND w.merchant_id=NEW.merchant_id AND w.url=NEW.destination_url\n      WHERE r.id=NEW.receipt_id AND r.merchant_id=NEW.merchant_id\n        AND ob.payload::jsonb=NEW.payload_snapshot::jsonb\n        AND e.payload::jsonb=NEW.payload_snapshot::jsonb\n        AND encode(public.digest(convert_to(NEW.destination_url, 'UTF8'), 'sha256'::text), 'hex')=NEW.destination_digest\n        AND encode(public.digest(convert_to(NEW.payload_snapshot::jsonb::text, 'UTF8'), 'sha256'::text), 'hex')=NEW.payload_digest\n    ) INTO valid;\n    IF NOT valid THEN RAISE EXCEPTION 'provider recipient intent does not match exact receipt outbox webhook event and snapshot'; END IF;\n  END IF;\n  RETURN NEW;\nEND $$"

def _provider_guard_v2():
    sql = BASE_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    sql = sql.replace("r.rail_type='cryptobot'", "r.rail_type IN ('cryptobot','xrocket','tgwallet')", 1)
    sql = sql.replace("a.network_identifier='cryptobot:' || NEW.provider_network", "a.network_identifier=r.rail_type || ':' || NEW.provider_network", 1)
    needle = "JOIN public.ledger_assets a ON a.id=o.ledger_asset_id"
    sql = sql.replace(needle, needle + "\n      LEFT JOIN public.provider_settlements s ON s.observation_id=o.id AND s.merchant_id=o.merchant_id", 1)
    sql = sql.replace("le.amount_atomic=o.observed_amount_atomic", "le.amount_atomic=COALESCE(s.settled_amount_atomic,o.observed_amount_atomic)")
    needle = "'amount_atomic',o.observed_amount_atomic::text\n          )"
    assert needle in sql
    sql = sql.replace(needle, needle + " || CASE WHEN s.observation_id IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('settled_amount_atomic',s.settled_amount_atomic::text) END", 1)
    return sql

def upgrade():
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "gateway_rail_connections",
        sa.Column("rail_id", uuid, primary_key=True),
        sa.Column("merchant_id", uuid, nullable=False),
        sa.Column("rail_type", sa.String(32), nullable=False),
        sa.Column("network", sa.String(64), nullable=False),
        sa.Column("account_identity", sa.String(64), nullable=False),
        sa.Column("credentials_digest", sa.String(64), nullable=False),
        sa.Column("receivable_account_id", uuid, nullable=False),
        sa.Column("payable_account_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id","rail_id"],["rails.merchant_id","rails.id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merchant_id","receivable_account_id"],["ledger_accounts.merchant_id","ledger_accounts.id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merchant_id","payable_account_id"],["ledger_accounts.merchant_id","ledger_accounts.id"],ondelete="RESTRICT"),
        sa.UniqueConstraint("rail_type","network","account_identity",name="uq_gateway_provider_account"),
        sa.CheckConstraint("rail_type IN ('cryptobot','xrocket','tgwallet')",name="ck_gateway_connection_type"),
        sa.CheckConstraint("length(account_identity)=64 AND length(credentials_digest)=64",name="ck_gateway_connection_digests"),
    )
    op.create_table(
        "gateway_invoice_options",
        sa.Column("invoice_id",uuid,primary_key=True),
        sa.Column("merchant_id",uuid,nullable=False),
        sa.Column("allowed_rails",postgresql.JSONB(),nullable=False),
        sa.Column("customer_telegram_id",sa.String(32),nullable=False),
        sa.Column("request_digest",sa.String(64),nullable=False),
        sa.ForeignKeyConstraint(["merchant_id","invoice_id"],["invoices.merchant_id","invoices.id"],ondelete="RESTRICT"),
        sa.CheckConstraint("length(request_digest)=64",name="ck_gateway_invoice_digest"),
    )
    op.create_table(
        "provider_settlements",
        sa.Column("observation_id",uuid,primary_key=True),
        sa.Column("merchant_id",uuid,nullable=False),
        sa.Column("settled_amount_atomic",sa.Numeric(),nullable=False),
        sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.ForeignKeyConstraint(["merchant_id","observation_id"],["provider_payment_observations.merchant_id","provider_payment_observations.id"],ondelete="RESTRICT"),
        sa.CheckConstraint("settled_amount_atomic > 0 AND settled_amount_atomic=trunc(settled_amount_atomic) AND CAST(settled_amount_atomic AS TEXT) <> 'NaN' AND settled_amount_atomic < power(10::numeric,78)",name="ck_provider_settlement_amount"),
    )
    op.create_table(
        "gateway_operations",
        sa.Column("id",uuid,primary_key=True),
        sa.Column("merchant_id",uuid,nullable=False),
        sa.Column("rail_id",uuid,nullable=False),
        sa.Column("operation_type",sa.String(16),nullable=False),
        sa.Column("invoice_id",uuid,nullable=True),
        sa.Column("ledger_asset_id",uuid,nullable=False),
        sa.Column("amount_atomic",sa.Numeric(),nullable=False),
        sa.Column("destination",sa.String(256),nullable=False),
        sa.Column("idempotency_key",sa.String(128),nullable=False),
        sa.Column("request_digest",sa.String(64),nullable=False),
        sa.Column("status",sa.String(32),nullable=False),
        sa.Column("provider_reference",sa.String(256),nullable=True),
        sa.Column("pay_url",sa.String(1024),nullable=True),
        sa.Column("mapping_id",uuid,nullable=True),
        sa.Column("failure_code",sa.String(64),nullable=True),
        sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.Column("updated_at",sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.ForeignKeyConstraint(["merchant_id","rail_id"],["rails.merchant_id","rails.id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rail_id"],["gateway_rail_connections.rail_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["merchant_id","invoice_id"],["invoices.merchant_id","invoices.id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ledger_asset_id"],["ledger_assets.id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mapping_id"],["provider_payment_mappings.id"],ondelete="RESTRICT"),
        sa.UniqueConstraint("merchant_id","operation_type","idempotency_key",name="uq_gateway_operation_request"),
        sa.UniqueConstraint("invoice_id",name="uq_gateway_invoice_operation"),
        sa.CheckConstraint("operation_type IN ('invoice','payout')",name="ck_gateway_operation_type"),
        sa.CheckConstraint("status IN ('submitting','pending','unknown','completed','failed','reconciliation_required')",name="ck_gateway_operation_status"),
        sa.CheckConstraint("(operation_type='invoice')=(invoice_id IS NOT NULL)",name="ck_gateway_operation_invoice"),
        sa.CheckConstraint("amount_atomic > 0 AND amount_atomic=trunc(amount_atomic) AND CAST(amount_atomic AS TEXT) <> 'NaN' AND amount_atomic < power(10::numeric,78)",name="ck_gateway_operation_amount"),
        sa.CheckConstraint("length(request_digest)=64 AND length(idempotency_key) BETWEEN 1 AND 128",name="ck_gateway_operation_identity"),
    )
    op.execute(_provider_guard_v2())
    op.execute("""CREATE FUNCTION public.gateway_fact_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE valid boolean;
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'gateway source identity is immutable'; END IF;
  IF TG_TABLE_NAME='gateway_rail_connections' THEN
    SELECT EXISTS(
      SELECT 1 FROM public.rails r
      JOIN public.ledger_accounts a ON a.id=NEW.receivable_account_id AND a.merchant_id=NEW.merchant_id
      JOIN public.ledger_accounts p ON p.id=NEW.payable_account_id AND p.merchant_id=NEW.merchant_id
      WHERE r.id=NEW.rail_id AND r.merchant_id=NEW.merchant_id AND r.rail_type=NEW.rail_type AND r.network=NEW.network
        AND r.is_active AND r.encrypted_credentials IS NOT NULL
        AND encode(public.digest(convert_to(r.encrypted_credentials,'UTF8'),'sha256'::text),'hex')=NEW.credentials_digest
        AND a.account_type='merchant_custodial_receivable' AND a.custody_type='merchant_custodial'
        AND p.account_type='merchant_payable' AND p.custody_type='merchant_liability'
        AND a.id<>p.id
    ) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'gateway connection identity does not match rail and tenant accounts'; END IF;
  ELSIF TG_TABLE_NAME='gateway_invoice_options' THEN
    IF jsonb_typeof(NEW.allowed_rails)<>'array' OR jsonb_array_length(NEW.allowed_rails)=0 THEN
      RAISE EXCEPTION 'invoice payment methods must be a nonempty array';
    END IF;
    SELECT NOT EXISTS(
      SELECT 1 FROM jsonb_array_elements_text(NEW.allowed_rails) x
      LEFT JOIN public.gateway_rail_connections c ON c.rail_id=x.value::uuid AND c.merchant_id=NEW.merchant_id
      WHERE c.rail_id IS NULL
    ) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'invoice payment methods cross tenant boundary'; END IF;
  ELSIF TG_TABLE_NAME='provider_settlements' THEN
    SELECT EXISTS(SELECT 1 FROM public.provider_payment_observations o
      WHERE o.id=NEW.observation_id AND o.merchant_id=NEW.merchant_id
        AND NEW.settled_amount_atomic<=o.observed_amount_atomic) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'provider settlement exceeds verified gross payment'; END IF;
  END IF;
  RETURN NEW;
END $$""")
    for table in ("gateway_rail_connections","gateway_invoice_options","provider_settlements"):
        op.execute(f"CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION public.gateway_fact_guard()")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()")
    op.execute("""CREATE FUNCTION public.gateway_operation_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'gateway operations cannot be deleted'; END IF;
  IF TG_OP='INSERT' AND NEW.status<>'submitting' THEN RAISE EXCEPTION 'operation must be persisted before provider submission'; END IF;
  IF TG_OP='UPDATE' THEN
    IF OLD.status IN ('completed','failed','reconciliation_required') THEN RAISE EXCEPTION 'terminal operation is immutable'; END IF;
    IF (NEW.id,NEW.merchant_id,NEW.rail_id,NEW.operation_type,NEW.invoice_id,NEW.ledger_asset_id,NEW.amount_atomic,NEW.destination,NEW.idempotency_key,NEW.request_digest,NEW.created_at)
      IS DISTINCT FROM (OLD.id,OLD.merchant_id,OLD.rail_id,OLD.operation_type,OLD.invoice_id,OLD.ledger_asset_id,OLD.amount_atomic,OLD.destination,OLD.idempotency_key,OLD.request_digest,OLD.created_at)
    THEN RAISE EXCEPTION 'operation request identity is immutable'; END IF;
    IF NEW.status='submitting' AND OLD.status<>'submitting' THEN RAISE EXCEPTION 'operation cannot be resubmitted'; END IF;
    IF (OLD.provider_reference IS NOT NULL AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference)
       OR (OLD.mapping_id IS NOT NULL AND NEW.mapping_id IS DISTINCT FROM OLD.mapping_id)
    THEN RAISE EXCEPTION 'operation provider binding is immutable'; END IF;
  END IF;
  IF NEW.mapping_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM public.provider_payment_mappings m
    WHERE m.id=NEW.mapping_id AND m.merchant_id=NEW.merchant_id AND m.invoice_id=NEW.invoice_id
      AND m.rail_id=NEW.rail_id AND m.provider_invoice_id=NEW.provider_reference
      AND m.ledger_asset_id=NEW.ledger_asset_id AND m.expected_amount_atomic=NEW.amount_atomic
  ) THEN RAISE EXCEPTION 'operation mapping binding mismatch'; END IF;
  IF NEW.operation_type='invoice' AND NEW.status='completed' AND NOT EXISTS(
    SELECT 1 FROM public.provider_payment_receipts pr JOIN public.provider_payment_observations o ON o.id=pr.observation_id
    WHERE o.mapping_id=NEW.mapping_id AND pr.merchant_id=NEW.merchant_id AND pr.invoice_id=NEW.invoice_id
  ) THEN RAISE EXCEPTION 'operation completion lacks verified receipt'; END IF;
  RETURN NEW;
END $$""")
    op.execute("CREATE TRIGGER gateway_operation_guard BEFORE INSERT OR UPDATE OR DELETE ON gateway_operations FOR EACH ROW EXECUTE FUNCTION public.gateway_operation_guard()")
    op.execute("CREATE TRIGGER gateway_operations_no_truncate BEFORE TRUNCATE ON gateway_operations FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()")
    op.create_index("ix_gateway_operations_poll","gateway_operations",["operation_type","status","created_at"])

def downgrade():
    count=op.get_bind().execute(sa.text("SELECT (SELECT count(*) FROM gateway_operations)+(SELECT count(*) FROM provider_settlements)+(SELECT count(*) FROM gateway_rail_connections)+(SELECT count(*) FROM gateway_invoice_options)")).scalar_one()
    if count:
        raise RuntimeError("refusing to discard durable financial identities; disable the feature and roll back code")
    op.execute(BASE_GUARD.replace("CREATE FUNCTION","CREATE OR REPLACE FUNCTION",1))
    for table in ("gateway_operations","provider_settlements","gateway_invoice_options","gateway_rail_connections"):
        op.drop_table(table)
    op.execute("DROP FUNCTION public.gateway_operation_guard()")
    op.execute("DROP FUNCTION public.gateway_fact_guard()")
