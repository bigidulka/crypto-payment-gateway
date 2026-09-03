# Gateway secret rotation and fresh address pool

Rotate compromised gateway secrets on production server and rebuild fresh deposit address pool.

## Goals
- Keep public payment services stopped during rotation.
- Backup DB and env before mutation.
- Rotate locally controllable secrets without printing secret values.
- Retire old deposit addresses and active sessions.
- Create fresh EVM deposit address pool with new HD seed/encryption key.
- Validate pool, invoice/address selection, and service readiness.

## Checklist
- [x] Confirm service freeze state.
- [x] Backup DB and server `.env`.
- [x] Inspect config variable names and compose dependencies without printing secrets.
- [x] Rotate generated secrets in `.env`.
- [x] Rotate PostgreSQL password if safe.
- [x] Retire old deposit address pool.
- [x] Cancel/expire old active payment sessions.
- [x] Reset Redis derivation index after old max index.
- [x] Generate fresh EVM pool.
- [x] Validate counts and no old active/available addresses.
- [x] Validate sample invoice/select gets fresh address.
- [x] Restart gateway services in controlled order if safe.
- [x] Deploy/restart patched payment_bot after gateway safe.

## Verification
- Server services before rotation: only `postgres` and `redis` running; payment services stopped.
- Server backups created:
  - `tmp/migration_backups/env_before_secret_rotation_20260622T135854Z.bak`
  - `tmp/migration_backups/arbitron_payment_before_secret_rotation_20260622T135854Z.dump` (`816K`)
- Local commit pushed: `0cc1acf chore: require postgres password from env`.
- Server pulled `0cc1acf`; `docker compose config` passed.
- Rotated generated local secrets without printing values: app/admin/encryption/HD/API/webhook/Postgres/funder env wiring/treasury addresses.
- Rotated SOLANA_WALLET_SEED and TON_WALLET_SEED without printing values.
- Retired old EVM pool: old total `30`, old max derivation index `29`, old non-retired after `0`.
- Fresh EVM pool generated: indices `30..229`, `200` available.
- Non-completed sweep jobs before/after pool work: `0`.
- Active sessions before/after: `0`.
- Encryption validation: new index `30` decrypts with new `ENCRYPTION_KEY` and matches public address.
- Invoice/select smoke: selected fresh index `30`; cleanup restored `200` available and deleted smoke invoice.
- Merchant API key DB rotated: active keys before `1`, after `1`; payment_bot `.env` synced from gateway `.env`; backup `/home/server/Projects/VERS2/payment_bot/.env.before_arbitron_key_rotation_20260622T141009Z.bak`.
- Gateway restarted at `1aeacff`: `api` healthy; `worker-poller`, `worker-webhook`, `worker-expirer`, `worker-sweeper` running; `worker-persistent` remains disabled by profile.
- Gateway API smoke with rotated Bearer key: `/v1/invoices` + `/pay/{public_id}/select` returned fresh address; smoke invoice deleted; pool restored to `200` available.
- payment_bot deployed at `e1fa8e5`; `manager_bot` and `bot_runner` rebuilt/restarted.
- Final DB counts: fresh available `200`, old non-retired `0`, active sessions `0`, non-completed sweeps `0`, active API keys `1`.
- Known non-rotation blocker: external provider secrets (`OKLINK_WEB_KEY`, RPC URL embedded provider keys, `TON_API_KEY`) require provider-side replacement; current values preserved to keep scanner online.
- Known runtime noise: gateway webhook DNS errors for existing invalid webhook URLs; payment_bot has pre-existing Telegram Unauthorized/chat-not-found warnings for some bot instances.

## Notes
- Do not print private keys, mnemonics, API keys, `.env` contents, or DB passwords.
- Public addresses and tx hashes may be printed.
- Server project: `/home/server/Projects/arbitron-payment`.
- Local canonical repo: `/home/fsdf1234/Projects/arbitron-payment`.
