"""Non-EVM identity sizes, immutable receipt proof and signed transfer attempts."""
from alembic import op
import sqlalchemy as sa

revision = '0015_onchain_operations'
down_revision = '0014_settlement_receipt_binding'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ck_ledger_account_type', 'ledger_accounts', type_='check')
    op.create_check_constraint('ck_ledger_account_type', 'ledger_accounts', "account_type IN ('gateway_treasury_pending','merchant_payable','merchant_custodial_receivable','suspense_unmatched','refund_liability','merchant_fee_revenue','provider_fee_expense','network_fee_expense','provider_expense','operator_gas_expense','operator_gas_cash')")
    for table, fields in {'onchain_txs': ('tx_hash','from_address','to_address','token_contract'),
            'unified_sweep_jobs': ('token_contract','from_address','to_address','gas_tx_hash','sweep_tx_hash')}.items():
        for field in fields:
            op.alter_column(table, field, type_=sa.String(128))
    op.execute('''CREATE TABLE onchain_receipts (
        id uuid PRIMARY KEY, merchant_id uuid NOT NULL REFERENCES merchants(id) ON DELETE RESTRICT,
        invoice_id uuid NOT NULL UNIQUE REFERENCES invoices(id) ON DELETE RESTRICT,
        session_id uuid NOT NULL UNIQUE REFERENCES payment_sessions(id) ON DELETE RESTRICT,
        asset_id uuid NOT NULL REFERENCES ledger_assets(id) ON DELETE RESTRICT,
        transaction_id uuid NOT NULL UNIQUE REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
        event_id varchar(64) NOT NULL UNIQUE, amount_atomic numeric NOT NULL CHECK(amount_atomic > 0 AND amount_atomic < 1e78 AND amount_atomic=trunc(amount_atomic)),
        evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE onchain_scan_cursors (session_id uuid PRIMARY KEY REFERENCES payment_sessions(id) ON DELETE RESTRICT, chain varchar(32) NOT NULL, last_block bigint NOT NULL CHECK(last_block>=0), updated_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE onchain_operations (
        id uuid PRIMARY KEY, merchant_id uuid NOT NULL REFERENCES merchants(id) ON DELETE RESTRICT,
        purpose varchar(16) NOT NULL CHECK(purpose IN ('payout','sweep','funding')),
        key varchar(128) NOT NULL, digest varchar(64) NOT NULL CHECK(length(digest)=64),
        chain varchar(32) NOT NULL, sender varchar(128) NOT NULL, destination varchar(128) NOT NULL,
        token varchar(128) NOT NULL, asset_id uuid REFERENCES ledger_assets(id) ON DELETE RESTRICT,
        amount_atomic numeric NOT NULL CHECK(amount_atomic>0 AND amount_atomic<1e78 AND amount_atomic=trunc(amount_atomic)),
        status varchar(32) NOT NULL CHECK(status IN ('preparing','pending','unknown','completed','failed','reconciliation_required')),
        tx_id varchar(128), encrypted_payload text, prepared_context jsonb,
        fee_budget_atomic numeric, fee_paid_atomic numeric,
        native_cost_atomic numeric, gas_transaction_id uuid UNIQUE REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
        last_broadcast_at timestamptz, wallet_released boolean NOT NULL DEFAULT false,
        transaction_id uuid UNIQUE REFERENCES ledger_transactions(id) ON DELETE RESTRICT,
        last_error varchar(128), created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_onchain_operation_key UNIQUE(merchant_id,key),
        CHECK ((tx_id IS NULL) = (encrypted_payload IS NULL)),
        CHECK (status NOT IN ('pending','completed') OR tx_id IS NOT NULL),
        CHECK (purpose<>'payout' OR asset_id IS NOT NULL))''')
    op.execute('CREATE UNIQUE INDEX uq_onchain_tx_identity ON onchain_operations(chain,tx_id) WHERE tx_id IS NOT NULL')
    op.execute('CREATE INDEX ix_onchain_operation_wallet ON onchain_operations(chain,sender,status)')
    op.execute('''CREATE FUNCTION public.onchain_fact_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    BEGIN
      IF TG_OP='DELETE' OR TG_TABLE_NAME='onchain_receipts' AND TG_OP='UPDATE' THEN RAISE EXCEPTION 'onchain fact is immutable'; END IF;
      IF TG_TABLE_NAME='onchain_operations' AND TG_OP='UPDATE' THEN
        IF (NEW.merchant_id,NEW.purpose,NEW.key,NEW.digest,NEW.chain,NEW.sender,NEW.destination,NEW.token,NEW.asset_id,NEW.amount_atomic)
          IS DISTINCT FROM (OLD.merchant_id,OLD.purpose,OLD.key,OLD.digest,OLD.chain,OLD.sender,OLD.destination,OLD.token,OLD.asset_id,OLD.amount_atomic)
          OR OLD.tx_id IS NOT NULL AND (NEW.tx_id,NEW.encrypted_payload,NEW.prepared_context,NEW.fee_budget_atomic)
            IS DISTINCT FROM (OLD.tx_id,OLD.encrypted_payload,OLD.prepared_context,OLD.fee_budget_atomic)
          OR OLD.status IN ('completed','failed') AND NEW IS DISTINCT FROM OLD
          OR OLD.wallet_released AND NOT NEW.wallet_released
        THEN RAISE EXCEPTION 'onchain operation binding is immutable'; END IF;
      END IF;
      IF TG_TABLE_NAME='onchain_receipts' AND TG_OP='INSERT' THEN
        IF NOT EXISTS(SELECT 1 FROM public.invoices i JOIN public.payment_sessions s ON s.invoice_id=i.id
          JOIN public.ledger_transactions t ON t.id=NEW.transaction_id JOIN public.invoice_events e ON e.invoice_id=i.id AND e.event_id=NEW.event_id
          JOIN public.ledger_assets a ON a.id=NEW.asset_id
          WHERE i.id=NEW.invoice_id AND i.merchant_id=NEW.merchant_id AND i.status::text='CONFIRMED'
            AND s.id=NEW.session_id AND s.status::text='paid' AND t.merchant_id=i.merchant_id AND t.status='posted'
            AND t.source_namespace='onchain_payment' AND t.source_external_id=s.chain||':'||s.id::text
            AND t.source_digest=NEW.evidence->>'digest'
            AND a.network_identifier=s.chain AND a.symbol=s.token
            AND e.event_type='invoice.confirmed'
            AND e.payload#>>'{observed,asset_id}'=NEW.asset_id::text
            AND (e.payload#>>'{observed,amount_atomic}')::numeric=NEW.amount_atomic)
          OR (SELECT count(*) FROM public.ledger_entries WHERE ledger_transaction_id=NEW.transaction_id)<>2
          OR (SELECT count(*) FROM public.ledger_entries e JOIN public.ledger_accounts a ON a.id=e.ledger_account_id
            WHERE e.ledger_transaction_id=NEW.transaction_id AND e.ledger_asset_id=NEW.asset_id
              AND e.amount_atomic=NEW.amount_atomic AND a.merchant_id=NEW.merchant_id
              AND ((a.account_type='merchant_payable' AND e.direction='credit')
                OR (a.account_type='gateway_treasury_pending' AND e.direction='debit')))<>2
        THEN RAISE EXCEPTION 'onchain receipt lacks journal/invoice proof'; END IF;
      END IF;
      IF TG_TABLE_NAME='onchain_operations' THEN
       IF NEW.status IN ('completed','failed') AND NEW.native_cost_atomic>0 THEN
        IF NOT EXISTS(SELECT 1 FROM public.ledger_transactions t WHERE t.id=NEW.gas_transaction_id
            AND t.merchant_id=NEW.merchant_id AND t.status='posted' AND t.source_namespace='onchain_gas'
            AND t.source_external_id=NEW.chain||':'||NEW.tx_id AND t.source_digest=NEW.digest)
          OR (SELECT count(*) FROM public.ledger_entries WHERE ledger_transaction_id=NEW.gas_transaction_id)<>2
          OR (SELECT count(*) FROM public.ledger_entries e JOIN public.ledger_accounts a ON a.id=e.ledger_account_id
            JOIN public.ledger_assets la ON la.id=e.ledger_asset_id
            WHERE e.ledger_transaction_id=NEW.gas_transaction_id AND la.is_native AND la.network_identifier=NEW.chain
              AND e.amount_atomic=NEW.native_cost_atomic AND a.merchant_id=NEW.merchant_id
              AND ((a.account_type='operator_gas_expense' AND e.direction='debit')
                OR (a.account_type='operator_gas_cash' AND e.direction='credit')))<>2
        THEN RAISE EXCEPTION 'onchain gas lacks operator journal proof'; END IF;
       END IF;
       IF NEW.status='completed' AND NEW.purpose='payout' THEN
        IF NOT EXISTS(SELECT 1 FROM public.ledger_transactions t WHERE t.id=NEW.transaction_id
            AND t.merchant_id=NEW.merchant_id AND t.status='posted' AND t.source_namespace='onchain_payout'
            AND t.source_external_id=NEW.chain||':'||NEW.tx_id AND t.source_digest=NEW.digest)
          OR (SELECT count(*) FROM public.ledger_entries WHERE ledger_transaction_id=NEW.transaction_id)<>2
          OR (SELECT count(*) FROM public.ledger_entries e JOIN public.ledger_accounts a ON a.id=e.ledger_account_id
            WHERE e.ledger_transaction_id=NEW.transaction_id AND e.ledger_asset_id=NEW.asset_id
              AND e.amount_atomic=NEW.amount_atomic AND a.merchant_id=NEW.merchant_id
              AND ((a.account_type='merchant_payable' AND e.direction='debit')
                OR (a.account_type='gateway_treasury_pending' AND e.direction='credit')))<>2
        THEN RAISE EXCEPTION 'onchain payout lacks journal proof'; END IF;
      END IF;
       END IF;
      RETURN NEW;
    END $$''')
    for table in ('onchain_receipts','onchain_operations'):
        op.execute(f'CREATE TRIGGER onchain_fact_guard BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION public.onchain_fact_guard()')


def downgrade():
    op.execute("DO $$ BEGIN IF EXISTS(SELECT 1 FROM onchain_receipts) OR EXISTS(SELECT 1 FROM onchain_operations) THEN RAISE EXCEPTION 'onchain facts prevent downgrade'; END IF; END $$")
    op.drop_table('onchain_scan_cursors')
    op.drop_table('onchain_receipts')
    op.drop_table('onchain_operations')
    op.execute('DROP FUNCTION public.onchain_fact_guard()')
    # Expanded identities are retained: narrowing could destroy historical evidence.
