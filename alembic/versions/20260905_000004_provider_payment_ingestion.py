"""Add disabled provider payment ingestion source facts and mappings.

Revision ID: 0011_provider_payment_ingestion
Revises: 0010_ledger_foundation

This migration only creates storage for a disabled internal verification path.
It does not enable provider I/O, invoice creation, ledger routing, backfill,
fees, payouts, webhooks, or payment lifecycle orchestration.
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_provider_payment_ingestion"
down_revision = "0010_ledger_foundation"


def upgrade() -> None:
    # Composite references below make every financial binding tenant explicit.
    op.create_unique_constraint("uq_invoice_tenant", "invoices", ["merchant_id", "id"])
    op.create_unique_constraint("uq_rail_tenant", "rails", ["merchant_id", "id"])
    op.add_column("invoice_events", sa.Column("event_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_invoice_event_event_id", "invoice_events", ["event_id"])
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "provider_payment_mappings",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("rail_id", sa.UUID(), nullable=False),
        sa.Column("provider_account_identity", sa.String(128), nullable=False),
        sa.Column("provider_network", sa.String(64), nullable=False),
        sa.Column("provider_invoice_id", sa.String(256), nullable=False),
        sa.Column("provider_asset_code", sa.String(32), nullable=False),
        sa.Column("ledger_asset_id", sa.UUID(), nullable=False),
        sa.Column("expected_amount_atomic", sa.Numeric(), nullable=False),
        sa.Column("receivable_account_id", sa.UUID(), nullable=False),
        sa.Column("payable_account_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "invoice_id"],
            ["invoices.merchant_id", "invoices.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "rail_id"], ["rails.merchant_id", "rails.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["merchant_id", "receivable_account_id"],
            ["ledger_accounts.merchant_id", "ledger_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "payable_account_id"],
            ["ledger_accounts.merchant_id", "ledger_accounts.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "provider_account_identity ~ '^[0-9a-f]{64}$'",
            name="ck_provider_mapping_account_identity",
        ),
        sa.CheckConstraint(
            "provider_network = btrim(provider_network) AND provider_network ~ '^[A-Z0-9_:-]{1,64}$'",
            name="ck_provider_mapping_network",
        ),
        sa.CheckConstraint(
            "provider_invoice_id = btrim(provider_invoice_id) AND provider_invoice_id <> ''",
            name="ck_provider_mapping_invoice_identity",
        ),
        sa.CheckConstraint(
            "provider_asset_code = upper(provider_asset_code) AND provider_asset_code <> ''",
            name="ck_provider_mapping_asset_code",
        ),
        sa.CheckConstraint(
            "CAST(expected_amount_atomic AS TEXT) <> 'NaN' AND expected_amount_atomic > 0 AND expected_amount_atomic < 100000000000000000000000000000000000000000000000000000000000000000000000000 AND expected_amount_atomic = trunc(expected_amount_atomic)",
            name="ck_provider_mapping_expected_atomic",
        ),
        sa.UniqueConstraint("merchant_id", "id", name="uq_provider_mapping_tenant"),
        sa.UniqueConstraint(
            "provider_account_identity",
            "provider_network",
            "provider_invoice_id",
            name="uq_provider_mapping_identity",
        ),
    )
    op.create_index(
        "ix_provider_mapping_invoice", "provider_payment_mappings", ["merchant_id", "invoice_id"]
    )

    op.create_table(
        "provider_payment_observations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("mapping_id", sa.UUID(), nullable=False),
        sa.Column("rail_id", sa.UUID(), nullable=False),
        sa.Column("provider_network", sa.String(64), nullable=False),
        sa.Column("provider_account_identity", sa.String(128), nullable=False),
        sa.Column("provider_invoice_id", sa.String(256), nullable=False),
        sa.Column("ledger_asset_id", sa.UUID(), nullable=False),
        sa.Column("observed_amount_atomic", sa.Numeric(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("economic_digest", sa.String(64), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "mapping_id"],
            ["provider_payment_mappings.merchant_id", "provider_payment_mappings.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["ledger_asset_id"], ["ledger_assets.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "CAST(observed_amount_atomic AS TEXT) <> 'NaN' AND observed_amount_atomic > 0 AND observed_amount_atomic < 100000000000000000000000000000000000000000000000000000000000000000000000000 AND observed_amount_atomic = trunc(observed_amount_atomic)",
            name="ck_provider_observation_atomic",
        ),
        sa.CheckConstraint(
            "economic_digest ~ '^[0-9a-f]{64}$'", name="ck_provider_observation_economic_digest"
        ),
        sa.CheckConstraint(
            "evidence_digest ~ '^[0-9a-f]{64}$'", name="ck_provider_observation_evidence_digest"
        ),
        sa.CheckConstraint(
            "source_kind IN ('provider_poll', 'provider_webhook')",
            name="ck_provider_observation_source",
        ),
        sa.UniqueConstraint("merchant_id", "id", name="uq_provider_observation_tenant"),
        sa.UniqueConstraint(
            "provider_account_identity",
            "provider_network",
            "provider_invoice_id",
            name="uq_provider_observation_identity",
        ),
    )

    op.create_table(
        "provider_verification_decisions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("decision_digest", sa.String(64), nullable=False),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "observation_id"],
            ["provider_payment_observations.merchant_id", "provider_payment_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "decision IN ('verified', 'reconciliation_required', 'rejected')",
            name="ck_provider_decision_value",
        ),
        sa.CheckConstraint(
            "decision_digest ~ '^[0-9a-f]{64}$'", name="ck_provider_decision_digest"
        ),
        sa.UniqueConstraint("observation_id", name="uq_provider_decision_observation"),
    )
    op.create_index(
        "ix_provider_decision_observation", "provider_verification_decisions", ["observation_id"]
    )
    op.create_unique_constraint(
        "uq_provider_decision_tenant", "provider_verification_decisions", ["merchant_id", "id"]
    )
    op.create_table(
        "provider_payment_receipts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("observation_id", sa.UUID(), nullable=False),
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("ledger_transaction_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("invoice_event_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("intended_recipient_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "observation_id"],
            ["provider_payment_observations.merchant_id", "provider_payment_observations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "decision_id"],
            ["provider_verification_decisions.merchant_id", "provider_verification_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "ledger_transaction_id"],
            ["ledger_transactions.merchant_id", "ledger_transactions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "invoice_id"],
            ["invoices.merchant_id", "invoices.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["invoice_event_id"], ["invoice_events.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "intended_recipient_count >= 0", name="ck_provider_receipt_intent_count"
        ),
        sa.CheckConstraint("event_id ~ '^[0-9a-f]{64}$'", name="ck_provider_receipt_event_id"),
        sa.UniqueConstraint("merchant_id", "id", name="uq_provider_receipt_tenant"),
        sa.UniqueConstraint("observation_id", name="uq_provider_receipt_observation"),
        sa.UniqueConstraint("event_id", name="uq_provider_receipt_event"),
    )
    op.create_table(
        "provider_payment_recipient_intents",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("receipt_id", sa.UUID(), nullable=False),
        sa.Column("outbox_id", sa.UUID(), nullable=False),
        sa.Column("webhook_id", sa.UUID(), nullable=False),
        sa.Column("destination_url", sa.String(500), nullable=False),
        sa.Column("payload_snapshot", sa.JSON(), nullable=False),
        sa.Column("destination_digest", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "receipt_id"],
            ["provider_payment_receipts.merchant_id", "provider_payment_receipts.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "destination_digest ~ '^[0-9a-f]{64}$'", name="ck_provider_intent_destination_digest"
        ),
        sa.CheckConstraint(
            "payload_digest ~ '^[0-9a-f]{64}$'", name="ck_provider_intent_payload_digest"
        ),
        sa.UniqueConstraint("receipt_id", "outbox_id", name="uq_provider_intent_receipt_outbox"),
        sa.UniqueConstraint("receipt_id", "webhook_id", name="uq_provider_intent_receipt_webhook"),
    )

    op.execute("""CREATE FUNCTION public.provider_payment_ingestion_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE valid boolean; locked_event public.invoice_events%ROWTYPE;
BEGIN
  IF TG_OP <> 'INSERT' THEN
    RAISE EXCEPTION 'provider payment ingestion source data is immutable';
  END IF;
  IF TG_TABLE_NAME = 'provider_payment_mappings' THEN
    SELECT EXISTS(
      SELECT 1
      FROM public.invoices i
      JOIN public.rails r ON r.id=NEW.rail_id AND r.merchant_id=NEW.merchant_id
      JOIN public.ledger_assets a ON a.id=NEW.ledger_asset_id
      JOIN public.ledger_accounts receivable ON receivable.id=NEW.receivable_account_id AND receivable.merchant_id=NEW.merchant_id
      JOIN public.ledger_accounts payable ON payable.id=NEW.payable_account_id AND payable.merchant_id=NEW.merchant_id
      WHERE i.id=NEW.invoice_id AND i.merchant_id=NEW.merchant_id
        AND i.status IN ('CREATED','AWAITING_PAYMENT')
        AND i.asset=NEW.provider_asset_code
        AND i.amount * power(10::numeric, a.atomic_decimals) = NEW.expected_amount_atomic
        AND r.rail_type='cryptobot' AND r.is_active AND r.network=NEW.provider_network
        AND r.assets::jsonb @> jsonb_build_array(NEW.provider_asset_code)
        AND a.network_kind='custodial' AND a.network_identifier='cryptobot:' || NEW.provider_network
        AND a.canonical_identifier=NEW.provider_asset_code AND NOT a.is_native
        AND receivable.account_type='merchant_custodial_receivable' AND receivable.custody_type='merchant_custodial'
        AND payable.account_type='merchant_payable' AND payable.custody_type='merchant_liability'
    ) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'provider mapping does not bind exact tenant canonical asset amount rail and accounts'; END IF;
  ELSIF TG_TABLE_NAME = 'provider_payment_observations' THEN
    SELECT EXISTS(
      SELECT 1 FROM public.provider_payment_mappings m
      WHERE m.id=NEW.mapping_id AND m.merchant_id=NEW.merchant_id
        AND m.rail_id=NEW.rail_id
        AND m.provider_account_identity=NEW.provider_account_identity
        AND m.provider_network=NEW.provider_network
        AND m.provider_invoice_id=NEW.provider_invoice_id
        AND m.ledger_asset_id=NEW.ledger_asset_id
    ) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'provider observation does not match its mapping'; END IF;
  ELSIF TG_TABLE_NAME = 'provider_payment_receipts' THEN
    SELECT * INTO locked_event FROM public.invoice_events WHERE id=NEW.invoice_event_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'provider receipt event missing'; END IF;
    SELECT EXISTS(
      SELECT 1
      FROM public.provider_payment_observations o
      JOIN public.provider_verification_decisions d ON d.id=NEW.decision_id AND d.merchant_id=NEW.merchant_id AND d.observation_id=NEW.observation_id AND d.decision='verified'
      JOIN public.provider_payment_mappings m ON m.id=o.mapping_id AND m.merchant_id=NEW.merchant_id
      JOIN public.ledger_assets a ON a.id=o.ledger_asset_id
      JOIN public.ledger_transactions t ON t.id=NEW.ledger_transaction_id AND t.merchant_id=NEW.merchant_id AND t.status='posted' AND t.source_namespace='provider_payment_observation' AND t.source_external_id=o.economic_digest AND t.source_digest=o.economic_digest
      JOIN public.invoices i ON i.id=NEW.invoice_id AND i.id=m.invoice_id AND i.merchant_id=NEW.merchant_id AND i.status='CONFIRMED'
      WHERE o.id=NEW.observation_id AND o.merchant_id=NEW.merchant_id
        AND locked_event.invoice_id=NEW.invoice_id AND locked_event.event_id=NEW.event_id AND locked_event.event_type='invoice.confirmed'
        AND NEW.event_id=o.economic_digest
        AND m.expected_amount_atomic=o.observed_amount_atomic
        AND m.receivable_account_id <> m.payable_account_id
        AND (SELECT count(*) FROM public.ledger_entries le WHERE le.ledger_transaction_id=t.id) = 2
        AND EXISTS(SELECT 1 FROM public.ledger_entries le WHERE le.ledger_transaction_id=t.id AND le.merchant_id=NEW.merchant_id AND le.ledger_account_id=m.receivable_account_id AND le.ledger_asset_id=o.ledger_asset_id AND le.direction='debit' AND le.amount_atomic=o.observed_amount_atomic)
        AND EXISTS(SELECT 1 FROM public.ledger_entries le WHERE le.ledger_transaction_id=t.id AND le.merchant_id=NEW.merchant_id AND le.ledger_account_id=m.payable_account_id AND le.ledger_asset_id=o.ledger_asset_id AND le.direction='credit' AND le.amount_atomic=o.observed_amount_atomic)
        AND locked_event.payload::jsonb=jsonb_build_object(
          'event','invoice.confirmed',
          'event_id',NEW.event_id,
          'invoice',jsonb_build_object('id',NEW.invoice_id::text,'public_id',i.public_id,'status','CONFIRMED'),
          'observed',jsonb_build_object(
            'ledger_asset_id',o.ledger_asset_id::text,
            'network_kind',a.network_kind,
            'network_identifier',a.network_identifier,
            'atomic_decimals',a.atomic_decimals,
            'provider_account_identity',o.provider_account_identity,
            'provider_network',o.provider_network,
            'asset_code',m.provider_asset_code,
            'amount_atomic',o.observed_amount_atomic::text
          )
        )
    ) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'provider receipt lacks exact verified journal event and amount projection'; END IF;
  ELSIF TG_TABLE_NAME = 'provider_payment_recipient_intents' THEN
    SELECT EXISTS(
      SELECT 1
      FROM public.provider_payment_receipts r
      JOIN public.invoice_events e ON e.id=r.invoice_event_id AND e.event_id=r.event_id
      JOIN public.outbox_webhooks ob ON ob.id=NEW.outbox_id AND ob.invoice_id=r.invoice_id AND ob.webhook_id=NEW.webhook_id AND ob.event_type='invoice.confirmed'
      JOIN public.webhooks w ON w.id=NEW.webhook_id AND w.merchant_id=NEW.merchant_id AND w.url=NEW.destination_url
      WHERE r.id=NEW.receipt_id AND r.merchant_id=NEW.merchant_id
        AND ob.payload::jsonb=NEW.payload_snapshot::jsonb
        AND e.payload::jsonb=NEW.payload_snapshot::jsonb
        AND encode(public.digest(convert_to(NEW.destination_url, 'UTF8'), 'sha256'::text), 'hex')=NEW.destination_digest
        AND encode(public.digest(convert_to(NEW.payload_snapshot::jsonb::text, 'UTF8'), 'sha256'::text), 'hex')=NEW.payload_digest
    ) INTO valid;
    IF NOT valid THEN RAISE EXCEPTION 'provider recipient intent does not match exact receipt outbox webhook event and snapshot'; END IF;
  END IF;
  RETURN NEW;
END $$""")
    op.execute("""CREATE FUNCTION public.provider_receipt_event_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.provider_payment_receipts r WHERE r.invoice_event_id=OLD.id) THEN
    RAISE EXCEPTION 'invoice event referenced by provider receipt is immutable';
  END IF;
  RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
END $$""")
    op.execute("""CREATE FUNCTION public.provider_payment_receipt_final_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE target_receipt_id uuid; expected_count integer; actual_count integer;
BEGIN
  IF TG_TABLE_NAME = 'provider_payment_receipts' THEN
    target_receipt_id := NEW.id;
  ELSIF TG_OP = 'DELETE' THEN
    target_receipt_id := (to_jsonb(OLD)->>'receipt_id')::uuid;
  ELSE
    target_receipt_id := (to_jsonb(NEW)->>'receipt_id')::uuid;
  END IF;
  SELECT r.intended_recipient_count INTO expected_count FROM public.provider_payment_receipts r WHERE r.id=target_receipt_id;
  IF expected_count IS NULL THEN RETURN NULL; END IF;
  SELECT count(*) INTO actual_count FROM public.provider_payment_recipient_intents i WHERE i.receipt_id=target_receipt_id;
  IF actual_count <> expected_count THEN RAISE EXCEPTION 'provider receipt recipient intent count mismatch'; END IF;
  RETURN NULL;
END $$""")
    for table in (
        "provider_payment_mappings",
        "provider_payment_observations",
        "provider_verification_decisions",
        "provider_payment_receipts",
        "provider_payment_recipient_intents",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION public.provider_payment_ingestion_guard()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION public.ledger_prevent_truncate()"
        )
    op.execute(
        "CREATE TRIGGER invoice_event_provider_receipt_guard BEFORE UPDATE OR DELETE ON invoice_events FOR EACH ROW EXECUTE FUNCTION public.provider_receipt_event_guard()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER provider_receipt_final AFTER INSERT OR UPDATE ON provider_payment_receipts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.provider_payment_receipt_final_guard()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER provider_intent_final AFTER INSERT OR UPDATE OR DELETE ON provider_payment_recipient_intents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.provider_payment_receipt_final_guard()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_financial_facts = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM public.provider_payment_observations) "
            "OR EXISTS (SELECT 1 FROM public.provider_verification_decisions) "
            "OR EXISTS (SELECT 1 FROM public.provider_payment_receipts) "
            "OR EXISTS (SELECT 1 FROM public.provider_payment_recipient_intents)"
        )
    ).scalar()
    if has_financial_facts:
        raise RuntimeError("refusing destructive provider ingestion downgrade with financial facts")
    op.execute(
        "DROP TRIGGER IF EXISTS invoice_event_provider_receipt_guard ON public.invoice_events"
    )
    op.execute("DROP FUNCTION IF EXISTS public.provider_receipt_event_guard()")
    op.execute("DROP FUNCTION IF EXISTS public.provider_payment_receipt_final_guard() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS public.provider_payment_ingestion_guard() CASCADE")
    op.drop_table("provider_payment_recipient_intents")
    op.drop_table("provider_payment_receipts")
    op.drop_constraint(
        "uq_provider_decision_tenant", "provider_verification_decisions", type_="unique"
    )
    op.drop_index("ix_provider_decision_observation", table_name="provider_verification_decisions")
    op.drop_table("provider_verification_decisions")
    op.drop_table("provider_payment_observations")
    op.drop_index("ix_provider_mapping_invoice", table_name="provider_payment_mappings")
    op.drop_table("provider_payment_mappings")
    op.drop_constraint("uq_invoice_event_event_id", "invoice_events", type_="unique")
    op.drop_column("invoice_events", "event_id")
    op.drop_constraint("uq_rail_tenant", "rails", type_="unique")
    op.drop_constraint("uq_invoice_tenant", "invoices", type_="unique")
