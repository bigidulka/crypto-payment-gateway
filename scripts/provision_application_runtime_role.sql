-- Production-candidate template. LOCAL/DISPOSABLE REHEARSAL ONLY.
-- Do not apply to production until a director approves role names, secret delivery,
-- migration-owner workflow, PUBLIC privilege inventory, backup and rollback.
--
-- Required psql variables (operator supplied; never committed):
--   application_runtime_role
--   application_runtime_password
--   migration_owner_role
--
-- DATABASE_URL is the one non-owner application connection. MIGRATION_DATABASE_URL
-- is a separate owner connection for external Alembic only; runtime containers
-- must never receive it or silently fall back to its credential.

BEGIN;
SELECT set_config('application.runtime_role', :'application_runtime_role', true);
SELECT set_config('application.migration_owner_role', :'migration_owner_role', true);
SELECT set_config('application.runtime_password', :'application_runtime_password', true);

DO $$
DECLARE
    runtime_name text := current_setting('application.runtime_role');
    migration_name text := current_setting('application.migration_owner_role');
    runtime_oid oid;
    database_oid oid := (SELECT oid FROM pg_database WHERE datname = current_database());
    existing boolean := false;
BEGIN
    SELECT oid INTO runtime_oid FROM pg_roles WHERE rolname = runtime_name;
    existing := FOUND;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = migration_name) THEN
        RAISE EXCEPTION 'migration owner role % does not exist', migration_name;
    END IF;

    IF existing THEN
        IF EXISTS (
            SELECT 1 FROM pg_roles
            WHERE oid = runtime_oid
              AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolinherit
                   OR rolreplication OR rolbypassrls OR NOT rolcanlogin)
        ) THEN
            RAISE EXCEPTION 'existing runtime role % is elevated, inheriting, or cannot login', runtime_name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_auth_members membership
            WHERE membership.member = runtime_oid
        ) THEN
            RAISE EXCEPTION 'existing runtime role % has role memberships', runtime_name;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_database WHERE oid = database_oid AND datdba = runtime_oid)
           OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner = runtime_oid)
           OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = runtime_oid)
           OR EXISTS (SELECT 1 FROM pg_proc WHERE proowner = runtime_oid)
        THEN
            RAISE EXCEPTION 'existing runtime role % owns database, schema, relation, or function', runtime_name;
        END IF;
        IF has_database_privilege(runtime_name, current_database(), 'CREATE')
           OR has_schema_privilege(runtime_name, 'public', 'CREATE')
           OR has_table_privilege(runtime_name, 'public.ledger_entries', 'TRUNCATE')
        THEN
            RAISE EXCEPTION 'existing runtime role % has effective CREATE or TRUNCATE privilege', runtime_name;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_proc function
            JOIN pg_namespace namespace ON namespace.oid = function.pronamespace
            WHERE namespace.nspname = 'public'
              AND function.prosecdef
              AND has_function_privilege(runtime_name, function.oid, 'EXECUTE')
        ) THEN
            RAISE EXCEPTION 'existing runtime role % can execute public security-definer function', runtime_name;
        END IF;
        IF COALESCE((SELECT rolconfig FROM pg_roles WHERE oid = runtime_oid), ARRAY[]::text[])
           IS DISTINCT FROM ARRAY['search_path=public, pg_catalog']
        THEN
            RAISE EXCEPTION 'existing runtime role % lacks exact controlled search_path', runtime_name;
        END IF;
        -- Existing credentials are intentionally not rotated or changed.
    ELSE
        EXECUTE format(
            'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
            runtime_name,
            current_setting('application.runtime_password')
        );
        runtime_oid := (SELECT oid FROM pg_roles WHERE rolname = runtime_name);
        EXECUTE format('ALTER ROLE %I SET search_path = public, pg_catalog', runtime_name);
    END IF;
END $$;

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), current_setting('application.runtime_role'))
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', current_setting('application.runtime_role'))
\gexec

-- Exact inventory from current mapped API/worker/ledger tables. No broad future
-- defaults: each migration must grant newly reviewed objects explicitly.
SELECT format(
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.address_lease_events, public.api_keys, public.chain_checkpoints, public.deposit_addresses, public.deposits, public.invoice_events, public.invoices, public.ledger_accounts, public.ledger_assets, public.ledger_entries, public.ledger_transactions, public.merchants, public.onchain_txs, public.outbox_webhooks, public.payment_sessions, public.rails, public.unified_sweep_jobs, public.user_balances, public.user_wallets, public.wallet_addresses, public.webhooks TO %I',
    current_setting('application.runtime_role')
)
\gexec

DO $$
DECLARE
    runtime_name text := current_setting('application.runtime_role');
BEGIN
    IF has_database_privilege(runtime_name, current_database(), 'CREATE')
       OR has_schema_privilege(runtime_name, 'public', 'CREATE')
       OR EXISTS (
           SELECT 1
           FROM pg_proc function
           JOIN pg_namespace namespace ON namespace.oid = function.pronamespace
           WHERE namespace.nspname = 'public'
             AND function.prosecdef
             AND has_function_privilege(runtime_name, function.oid, 'EXECUTE')
       )
       OR EXISTS (
           SELECT 1
           FROM unnest(ARRAY[
               'address_lease_events', 'api_keys', 'chain_checkpoints',
               'deposit_addresses', 'deposits', 'invoice_events', 'invoices',
               'ledger_accounts', 'ledger_assets', 'ledger_entries',
               'ledger_transactions', 'merchants', 'onchain_txs',
               'outbox_webhooks', 'payment_sessions', 'rails',
               'unified_sweep_jobs', 'user_balances', 'user_wallets',
               'wallet_addresses', 'webhooks'
           ]) table_name
           WHERE has_table_privilege(runtime_name, 'public.' || table_name, 'TRUNCATE')
       )
    THEN
        RAISE EXCEPTION 'runtime role % has effective CREATE, TRUNCATE, or public security-definer EXECUTE privilege', runtime_name;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_sequences WHERE schemaname = 'public') THEN
        RAISE EXCEPTION 'public sequences exist; extend reviewed runtime grant matrix explicitly';
    END IF;
END $$;

-- No ALTER DEFAULT PRIVILEGES. The transaction-local password GUC disappears at
-- COMMIT; existing roles retain their credential unchanged.
COMMIT;

-- Post-apply verification (read-only; run separately):
-- SELECT current_setting('search_path'),
--        has_database_privilege(:'application_runtime_role', current_database(), 'TEMPORARY'),
--        has_database_privilege(:'application_runtime_role', current_database(), 'CREATE'),
--        has_schema_privilege(:'application_runtime_role', 'public', 'CREATE'),
--        has_table_privilege(:'application_runtime_role', 'public.ledger_entries', 'TRUNCATE'),
--        COALESCE((SELECT string_agg(parent.rolname, ',') FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=:'application_runtime_role'), 'none');
