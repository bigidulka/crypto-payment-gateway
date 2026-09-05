# Webhook egress and onboarding hardening — checkpoint

Date: 2026-09-05
Status: local-only, **not ready for acceptance yet**. No commit, push, deploy, migration, production record change, replay, provider account or money movement occurred.

## ADR

`.ralph/webhook-egress-design.md` now selects public-only canonical HTTPS egress. No internal override/allowlist exists in this release; existing broken `host.docker.internal:8080` rows are not changed, replayed or exempted.

## Implemented in the current local worktree

* `src/core/webhook_egress.py`: URL syntax policy, strict ASCII-IDNA canonical host handling, all-answer global IP validation including IPv4-mapped IPv6, typed transient DNS errors, and per-delivery aiohttp resolver pinning.
* `src/workers/webhook_dispatcher.py`: one `asyncio.timeout` budget intended to cover DNS plus HTTP delivery, disabled redirects, bounded response read, non-sensitive delivery codes, and restored `processed += 1` in outbox processing.
* `src/api/merchant/{schemas.py,router.py}`, `src/services/webhook_service.py`, `src/api/admin/{schemas.py,router.py}`: common webhook URL request type, pre-storage DNS validation, typed onboarding models with forbidden extras, and 409 conversion for concurrent unique email conflict.
* `tests/test_webhook_egress.py`: policy/DNS/dispatcher/onboarding regressions.

## Required before acceptance

1. Finish and pass real local TLS transport coverage using a controlled HTTPS server, resolver-pinned loopback peer only in the test, trusted test CA, certificate/SNI success, certificate hostname mismatch failure, redirect not followed, and policy refusal before a mixed public/private DNS candidate can open a socket.
2. Rerun targeted tests with exact exit code.
3. Rerun guarded full safe suite using the documented explicit disposable PostgreSQL environment; record exact skips/warnings and exit code.

No claim of readiness is made in this checkpoint.

## Completed review corrections before full suite

* Policy is now strictly public HTTPS DNS only: the unready internal allowlist/config was removed, including any path that could bypass all non-global answers.
* DNS lookup is bounded and typed: timeout/no answer/gaierror becomes `WebhookResolutionError`, while non-public/all-answer policy rejection remains `WebhookEgressError`. Registration maps transient resolution to 503 and policy rejection to 422; dispatcher maps it to a safe retryable `dns_resolution_failed` result instead of aborting a batch.
* Canonical host is ASCII IDNA with trailing dot removed and strict `[a-z0-9-]` labels, matching URL storage, DNS resolver comparison and TLS SNI. Control/CRLF, backslash, zone, IP literal and explicit port zero reject.
* Dispatcher now has a single `asyncio.timeout` scope over DNS, TLS/connect, request and bounded response read; it restores `processed += 1` for every handled outbox item.
* Typed onboarding forbids unknown fields, including password/secret fields, and email validation rejects multi-`@`, whitespace and malformed domains.
* Real local transport tests generate an ephemeral localhost certificate with `cryptography`, run an aiohttp HTTPS server on loopback, pin the resolver peer, trust only the generated certificate, prove certificate/SNI success for `localhost`, prove hostname mismatch raises `ClientConnectorCertificateError`, and prove a 302 is not followed. This controlled loopback transport does not relax production public-IP policy.

Targeted evidence after corrections:

```text
WEBHOOK_TLS_CORRECTED_EXIT=0
29 passed, 3 warnings
```

Warnings are existing Pydantic v2 class-based-config deprecations in merchant schemas. Next and final gate for this turn is the guarded full safe suite.

## Final verification

* Real local TLS transport: ephemeral localhost certificate, trusted test CA, resolver-pinned loopback peer, correct hostname/SNI certificate success, hostname mismatch rejection, and redirect not followed. No production IP policy is relaxed by this test.
* Anvil test fixture now closes local adapters/fetcher provider sessions; isolated Anvil run has no `Unclosed client session` warning (`ANVIL_GLOBAL_CLEANUP_EXIT=0`).
* Static checks: `WEBHOOK_FINAL_STATIC_EXIT=0`.
* Targeted webhook/TLS/Anvil/Rails/SDK suite: `102 passed, 3 skipped, 13 warnings`, `WEBHOOK_FINAL_TARGETED_EXIT=0`.
* Real PostgreSQL concurrent onboarding acceptance: two independently committed sessions submit the same normalized email; exactly one merchant and one API key exist, one response succeeds and one is 409. `31 passed, 3 warnings`, `ONBOARDING_PROCESSING_ACCEPTANCE_EXIT=0`.
* Outbox processing regression verifies `processed == 2`, first DNS failure is recorded safely, and second delivery is still marked sent.
* Final guarded disposable PostgreSQL full safe suite, excluding only real-value-moving `tests/test_e2e_chains.py`: `255 passed, 36 skipped, 20 warnings`, `FINAL_ONBOARDING_GUARDED_SUITE_EXIT=0`.

Skip reasons: six public-RPC smoke tests skip only when provider unavailable; twenty-seven disabled non-EVM tests; three opt-in isolated Rails DB tests run only under their specialized explicit environment. Warnings: one non-test dataclass collection warning, three pre-existing Pydantic class-config deprecations, sixteen aiohttp `enable_cleanup_closed` deprecations. No unclosed-client-session warning remains.

## Final local status

This patch remains local-only and uncommitted: no deploy, push, migration, existing-webhook modification, replay, provider account or monetary action occurred. Existing broken internal callback rows remain in the database untouched. Public-only policy is ready for director review; financial ledger, rail orchestration, non-EVM and future explicitly-designed internal receiver policy remain out of scope.
