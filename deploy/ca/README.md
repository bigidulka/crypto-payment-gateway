# CA release and state authority

Application source is the observed RU commit `58dc6db9399ef5a177fe47eabf2751c86cab0fc8`,
not the diverged default branch. Every one of the five running roles matched
all 86 inventoried application modules and static files. Resolved packages
are pinned from that runtime; the Python base is pinned to 3.12.15.

Configuration is external and role-specific. Runtime must use `arbitron_runtime`,
with no superuser, CREATE DATABASE or CREATE ROLE rights, and no migration-owner
URL. PostgreSQL major 16, the live schema, object owners, sequences, cursors,
ledger and all unique/idempotency constraints must survive native restore.
Redis retains AOF persistence. Its CA credential is newly generated and must
match all five consumers; the old RU credential is preserved until source fence.
No password belongs in Compose, image metadata or command arguments.

All services have explicit profiles. Default Compose starts no application.
State services publish no host ports; the API binds only `127.0.0.1:8123`.
Never enable `api` or `financial` on preparation state. Before enabling them:

1. Pause every old request/webhook/Telegram input and prove in-flight work drained.
2. Stop and disable every old financial writer; reconcile uncertain provider sends.
3. Capture final native PG/roles/sequences, Redis AOF/RDB and all mounted data.
4. Verify complete restored row/ledger/queue/cursor digests and limited-role login.
5. Verify actual ingress, callback targets and dependent client routes.
6. Start exactly one CA API and one worker set, then verify existing receipts.

No new invoice, wallet transfer, refund or provider-paid test is an acceptance
probe. Once CA writes start, rollback uses current CA state. Stale RU databases
and stopped old workers cannot be restarted as authority.

Current migration authority, receipts and remaining gates are maintained in
`/home/fsdf1234/Projects/ovh-migration/STATE.json`, rather than inferred from this
staging configuration or a healthy container.
