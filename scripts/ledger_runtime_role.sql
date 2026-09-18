-- Local/disposable rehearsal template only. Do not apply to production without
-- director-approved role names, migration-owner workflow, backup and rollout.
--
-- Required psql variables, supplied by the operator (never committed):
--   ledger_runtime_role  quoted identifier text
--   ledger_runtime_password literal secret supplied at invocation
--
-- TEMPORARY is granted to PUBLIC by default. The role cannot be individually
-- denied while PUBLIC retains it. This disposable template revokes TEMPORARY
-- from PUBLIC solely to demonstrate the target effective privilege posture.
-- Production must not change PUBLIC without a reviewed inventory/regrant plan.

BEGIN;
SELECT set_config('ledger.runtime_role', :'ledger_runtime_role', false);
DO $$
BEGIN
    IF current_database() !~ '^test_' THEN
        RAISE EXCEPTION 'local rehearsal template requires a test_* database';
    END IF;
END $$;

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'ledger_runtime_role',
    :'ledger_runtime_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ledger_runtime_role')
\gexec

SELECT format('ALTER ROLE %I SET search_path = public, pg_catalog', :'ledger_runtime_role')
\gexec

SELECT format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database())
\gexec
SELECT format(
    'REVOKE TEMPORARY ON DATABASE %I FROM %I',
    current_database(),
    current_setting('ledger.runtime_role')
)
\gexec
SELECT format('REVOKE CREATE ON SCHEMA public FROM %I', current_setting('ledger.runtime_role'))
\gexec
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I',
    current_database(),
    current_setting('ledger.runtime_role')
)
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', current_setting('ledger.runtime_role'))
\gexec
SELECT format(
    'GRANT SELECT, INSERT, UPDATE ON TABLE public.merchants, public.invoices, public.rails, public.ledger_assets, public.ledger_accounts, public.ledger_transactions, public.ledger_entries, public.webhooks, public.outbox_webhooks, public.invoice_events, public.provider_payment_mappings, public.provider_payment_observations, public.provider_verification_decisions, public.provider_payment_receipts, public.provider_payment_recipient_intents, public.alembic_version TO %I',
    current_setting('ledger.runtime_role')
)
\gexec

-- Rehearsal verification query (read-only; operator may run after COMMIT):
-- SELECT has_database_privilege(:'ledger_runtime_role', current_database(), 'TEMPORARY'),
--        has_schema_privilege(:'ledger_runtime_role', 'public', 'CREATE'),
--        (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.ledger_transactions'::regclass),
--        ARRAY(SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=:'ledger_runtime_role');

COMMIT;
