# ADR: безопасный egress webhook

## Context

Webhook URL является merchant-controlled egress target. Исторические записи не должны автоматически меняться, replay-иться или использоваться как implicit allowlist. Ops подтвердил, что current failed `host.docker.internal:8080` callbacks не имеют работающего receiver; будущая policy не должна silently preserve them.

## Decision

1. Registration accepts only canonical `https://` DNS-hostname URLs with a public port (`443` default or explicit valid port). Host is stored as ASCII IDNA with trailing dot removed, matching DNS, TLS/SNI and resolver pinning.
2. URL with userinfo, fragment, literal IP, zone, control/CRLF, backslash, malformed label, non-HTTPS scheme or special/non-global DNS answer is rejected before storage. Existing database records are not changed by this patch.
3. This release is **public-only**. There is no internal override or allowlist. A future internal receiver proposal must bind exact host:port to explicit approved IP/CIDR policy and never implicitly permit link-local, multicast, metadata or `host.docker.internal` peers.
4. Before each delivery resolver receives **all** A/AAAA answers. Every answer must be globally routable; IPv4-mapped IPv6 is classified by embedded IPv4. The custom resolver supplies exactly those validated peers to the connector.
5. Dispatcher uses `aiohttp`, not `httpx`, for delivery. A per-request/per-connection custom `aiohttp.abc.AbstractResolver` returns the validated resolved peer addresses to `TCPConnector`; connection is made to that resolver result while HTTP Host/SNI remains the canonical hostname. This avoids unsafe "resolve-check then a different client resolves" TOCTOU and avoids global DNS monkeypatching/private httpx internals.
6. One `asyncio.timeout` deadline covers DNS, TLS/connect, request write and bounded response read. Redirects are disabled. Response body is drained/read only up to a fixed small cap and is not logged. Signature/secret headers, body and response content are never logged.

## Alternatives considered

### `httpx` plus preliminary DNS validation

Rejected: default `httpx` transport re-resolves hostname when opening the connection, so validated DNS answers are not necessarily the connected peer.

### Custom `httpx` transport / global resolver monkeypatch

Rejected: public httpx API does not expose a stable DNS-resolver hook matching this need. Patching global resolver/socket behavior affects unrelated requests and is not safely scoped.

### `aiohttp` custom resolver and `TCPConnector`

Selected: aiohttp exposes resolver/connector interfaces. The connector uses supplied resolver results for its connection attempt, so egress peer selection stays bound to validated answers without global process mutation. Resolver remains policy-owned and testable with injected lookup data.

## Compatibility and rollout

* New public webhook registrations require HTTPS/public DNS now.
* Existing records stay stored and are not replayed, deleted or automatically allowlisted.
* On a later dispatcher rollout, a legacy/internal record denied by policy receives a non-sensitive blocked-reason state in the outbox attempt path; it is not sent. Exact persistence policy/migration is out of scope because existing `last_error` is sufficient for a bounded reason and no schema migration is required.
* `host.docker.internal` is not enabled. An actual internal receiver and an approved future exact host:port plus IP/CIDR design are prerequisites to any exception.

## Residual limits

DNS pinning is per connection/delivery and protects endpoint selection. TLS certificate validation remains enabled. This does not solve receiver-side replay/idempotency; signed event and merchant-side reconciliation requirements remain separate gateway contracts.
