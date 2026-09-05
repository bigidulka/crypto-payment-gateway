# Runtime container context and readiness P0 correction

Date: 2026-09-05
Status: local implementation in progress. No commit, push, production image/container inspection beyond presence-only approved metadata, production role/grant/credential action, deployment, restart, migration, provider call, sweep, payout, or financial feature activation is authorized.

## Scope for this checkpoint

1. Replace broad Docker build context copying with a reviewable allowlist plus `.dockerignore`; prove a dummy owner marker cannot enter the built image without using real secrets.
2. Ensure runtime configuration never loads a baked `.env`, and add actual curated runtime env content validation for forbidden owner/migration/bootstrap key names.
3. Make `/ready` return generic `503` on database/Redis failure; retain `/health` liveness semantics. Limited compose health check must target `/ready`.
4. Consolidate limited-role entrypoint admission into reusable runtime code used before API/worker execution and before application lifespan side effects; do not duplicate privilege SQL in shell heredocs.
5. Attempt container admission evidence only after the bounded Docker/context and readiness patch. If the candidate image cannot build due environment network, record that exact blocker without claiming container readiness.

## Explicitly deferred

Production `PUBLIC` inventory/regrant, production credentials, migration owner procedure, real production role rollout, external provider/network activity, ledger lifecycle integration, fee/backfill/payout work, and any production deployment.

## Final local candidate evidence

The implemented candidate uses an allowlisted runtime image (`src`, canonical chain config, entrypoint only), separate explicit migration target, `.dockerignore`, no implicit dotenv loading, curated limited-role Compose override, entrypoint admission before application command execution, and generic `/ready` dependency failures while `/health` remains liveness.

The earlier bridge-network build failure was resolved only for the local diagnostic by `docker build --network=host`; no mirror, proxy, TLS, signature, daemon, or firewall setting changed. The exact Dockerfile then built distinct runtime and migration candidate images. A dummy secret value and a separate owner-url marker were absent from runtime inspect configuration, history, and exported filesystem.

The final local gate additionally rendered the **actual** `docker-compose.yml` plus `docker-compose.runtime-role.yml` using temporary dummy operator and curated runtime env files. It verified all five runtime service names exclude owner/migration/bootstrap values, retain their image entrypoint, and use `/ready` for API health. The unique internal-network harness then ran entrypoint-preserving short markers for each service and rejected owner URL, migration URL, protected-table `TRUNCATE`, and executable public SECURITY DEFINER drift before the marker.

Exact final results and separate isolated `0009`/`0010` regression evidence are in `.ralph/runtime-final-acceptance.md`. The old worker-loop batch timed out and remains explicitly not passed. No production action is claimed or authorized.
