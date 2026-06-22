# Per-check Address Lease Deploy + Live Canary Plan

## Preconditions
- Use full freeze unless user explicitly accepts worker-only risk.
- Do not edit source on server.
- Resolve local/server dirty state before deploy.
- Commit + push from local/GitHub first; server only pulls/rebuilds.
- Do not print `.env` or secrets.

## Recommended Freeze

Full freeze, safest:

```bash
cd /home/server/Projects/arbitron-payment
docker compose stop api worker-persistent worker-poller worker-sweeper worker-webhook worker-expirer
```

Rollback/restart old state:

```bash
cd /home/server/Projects/arbitron-payment
docker compose up -d api worker-persistent worker-poller worker-sweeper worker-webhook worker-expirer
```

## Backup + State Export

```bash
cd /home/server/Projects/arbitron-payment
mkdir -p tmp/migration_backups
docker exec arbitron-payment-postgres pg_dump -U arbitron -d arbitron_payment -Fc > tmp/migration_backups/arbitron_payment_$(date -u +%Y%m%dT%H%M%SZ).dump
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -Atc "select chain,address,last_scanned_block from wallet_addresses where is_active=true order by chain,address" > tmp/migration_backups/active_wallet_addresses_$(date -u +%Y%m%dT%H%M%SZ).tsv
```

## Pre-cutover Read-only Checks

```bash
cd /home/server/Projects/arbitron-payment
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -c "select chain,count(*) from wallet_addresses where is_active=true group by chain order by chain;"
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -c "select source,state,count(*) from unified_sweep_jobs group by source,state order by source,state;"
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -c "select status,count(*) from deposits group by status order by status;"
```

## Deploy Sequence

1. Local: ensure targeted tests/lint pass.
2. Local: commit migration/code/tests.
3. Local: push to GitHub main.
4. Server:

```bash
cd /home/server/Projects/arbitron-payment
git fetch origin
git status --short
git pull --ff-only origin main
docker compose build api worker-persistent worker-poller worker-sweeper worker-webhook worker-expirer
docker compose run --rm api alembic upgrade head
```

5. Start DB-safe non-scanners first if needed:

```bash
docker compose up -d api worker-webhook worker-expirer
```

6. Start scanner/sweeper after migration sanity:

```bash
docker compose up -d worker-poller worker-persistent worker-sweeper
```

## Post-migration Sanity

```bash
cd /home/server/Projects/arbitron-payment
docker compose ps
docker compose logs --tail=100 api worker-poller worker-persistent worker-sweeper worker-expirer

docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -c "select lease_status,count(*) from deposit_addresses group by lease_status order by lease_status;"
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -c "select status,count(*) from payment_sessions group by status order by status;"
docker exec arbitron-payment-postgres psql -U arbitron -d arbitron_payment -c "select event_type,count(*) from address_lease_events group by event_type order by event_type;"
```

## Legacy Backfill / Drain

Dry-run fixed ranges first:

```bash
docker compose run --rm api python scripts/legacy_backfill.py --chain bsc --from-block <FROM> --to-block <TO>
docker compose run --rm api python scripts/legacy_sweep_drain.py --chain bsc
```

Execute only after dry-run output is sane:

```bash
docker compose run --rm api python scripts/legacy_backfill.py --chain bsc --from-block <FROM> --to-block <TO> --execute
docker compose run --rm api python scripts/legacy_sweep_drain.py --chain bsc --create-missing-jobs --execute
```

## Live BSC Canary

1. Create one low-value BSC USDT invoice/check via merchant API or existing bot path.
2. Confirm `payment_sessions.status='pending'` and one `deposit_addresses.lease_status='leased'`.
3. Send exact small BSC USDT amount to leased address.
4. Watch worker-poller logs for OKLink detection.
5. Verify:
   - `onchain_txs` row inserted.
   - `payment_sessions.status` transitions `pending -> seen_onchain -> paid`.
   - `invoices.status` transitions to `CONFIRMED`.
   - `unified_sweep_jobs` row exists with `source='invoice'`.
   - `deposit_addresses.lease_status='cooldown'` and `cooldown_until` set.
   - webhook outbox created/sent.
6. Watch sweeper until job `completed` or known gas-funding pending state.
7. After cooldown, run a controlled second invoice and confirm address can be reused only after cooldown completion.

## Late Deposit Canary

1. Create low-value invoice/check.
2. Let it expire; verify payment session `expired`, address `cooldown`.
3. Send exact small amount during cooldown.
4. Recheck late-detection path before enabling automated handling; expected policy: invoice stays expired, session becomes `late`, sweep job created for manual reconciliation.

## Rollback Notes

- If migration not applied: restart old services from existing image.
- If migration applied: do not downgrade blindly with funds in leased/cooldown state; stop workers, restore DB dump if no post-migration funds were received.
- If canary funds received, prefer forward fix over DB restore unless operator confirms manual reconciliation.
