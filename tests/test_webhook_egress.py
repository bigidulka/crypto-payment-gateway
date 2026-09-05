import asyncio
import socket
import ssl
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientConnectorCertificateError, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

from src.api.admin.schemas import MerchantCreateRequest
from src.api.merchant.schemas import WebhookCreateRequest
from src.core.webhook_egress import (
    PinnedWebhookResolver,
    WebhookDestination,
    WebhookEgressError,
    WebhookResolutionError,
    pinned_webhook_session,
    resolve_webhook_destination,
    validate_webhook_url,
)


def test_webhook_schema_requires_https_dns_host_without_userinfo_or_fragment():
    assert (
        WebhookCreateRequest(url="https://merchant.example/webhook").url
        == "https://merchant.example/webhook"
    )

    for url in (
        "http://merchant.example/webhook",
        "https://user:pass@merchant.example/webhook",
        "https://merchant.example/webhook#fragment",
        "https://127.0.0.1/webhook",
        "https://[::1]/webhook",
        "https://merchant.example:0/webhook",
        "https://merchant.example\\bad/webhook",
        "https://merchant.example/\r\nheader: bad",
    ):
        with pytest.raises(ValidationError):
            WebhookCreateRequest(url=url)

@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.1/webhook",
        "https://[::ffff:127.0.0.1]/webhook",
        "https://merchant.example/webhook#x",
        "https://user:pass@merchant.example/webhook",
        "https://merchant.example:0/webhook",
        "https://fe80::1%25eth0/webhook",
        "https://merchant_example.example/webhook",
        "https://merchant!name.example/webhook",
        "https://merchant name.example/webhook",
    ],
)
def test_webhook_url_policy_rejects_special_literals_and_sensitive_url_parts(url):
    with pytest.raises(WebhookEgressError):
        validate_webhook_url(url)


async def test_dns_requires_every_answer_to_be_public():
    async def mixed_lookup(_host, _port):
        return [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET6, "::ffff:127.0.0.1")]

    with pytest.raises(WebhookEgressError, match="non-public"):
        await resolve_webhook_destination("https://merchant.example/webhook", lookup=mixed_lookup)


async def test_dns_pins_all_validated_public_answers():
    async def public_lookup(_host, _port):
        return [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET6, "2001:4860:4860::8888")]

    destination = await resolve_webhook_destination(
        "https://merchant.example/webhook", lookup=public_lookup
    )
    resolver = PinnedWebhookResolver(destination)
    resolved = await resolver.resolve("merchant.example", 443)

    assert {item["host"] for item in resolved} == {"8.8.8.8", "2001:4860:4860::8888"}
    with pytest.raises(OSError, match="unexpected"):
        await resolver.resolve("other.example", 443)


async def test_internal_dns_is_always_rejected_without_unready_override():
    async def private_lookup(_host, _port):
        return [(socket.AF_INET, "127.0.0.1")]

    with pytest.raises(WebhookEgressError, match="non-public"):
        await resolve_webhook_destination(
            "https://internal.example:8443/webhook",
            lookup=private_lookup,
        )


def test_idna_and_trailing_dot_are_canonicalized_for_url_and_resolver():
    parsed = validate_webhook_url("https://BÜCHER.example./webhook")
    assert parsed.hostname == "xn--bcher-kva.example"


async def test_dns_timeout_and_gaierror_are_typed_transient_failures():
    async def stalled_lookup(_host, _port):
        await asyncio.sleep(0.1)
        return []

    async def failing_lookup(_host, _port):
        raise socket.gaierror("not found")

    with pytest.raises(WebhookResolutionError, match="timed out"):
        await resolve_webhook_destination(
            "https://merchant.example/webhook",
            lookup=stalled_lookup,
            timeout_seconds=0.01,
        )
    with pytest.raises(WebhookResolutionError, match="failed"):
        await resolve_webhook_destination(
            "https://merchant.example/webhook",
            lookup=failing_lookup,
        )

def test_typed_merchant_onboarding_normalizes_only_safe_fields():
    payload = MerchantCreateRequest(
        name="  Merchant  ",
        email=" User@Example.COM ",
        key_name=" primary ",
    )
    assert payload.name == "Merchant"
    assert payload.email == "user@example.com"
    assert payload.key_name == "primary"

    with pytest.raises(ValidationError):
        MerchantCreateRequest(name="Merchant", email="not-an-email")


@pytest.mark.parametrize(
    "email",
    ["a@@example.com", "a b@example.com", "a@example", "a@example..com"],
)
def test_typed_merchant_onboarding_rejects_ambiguous_email(email):
    with pytest.raises(ValidationError):
        MerchantCreateRequest(name="Merchant", email=email)


def test_typed_merchant_onboarding_forbids_unknown_secret_fields():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MerchantCreateRequest(
            name="Merchant",
            email="merchant@example.test",
            password="must-not-be-accepted",
        )


async def test_dispatcher_handles_dns_failure_without_aborting_later_delivery(monkeypatch):
    from src.workers import webhook_dispatcher

    calls = 0

    async def resolver(url, **_kwargs):
        nonlocal calls
        calls += 1
        if "broken" in url:
            raise WebhookResolutionError("webhook DNS lookup failed")
        return WebhookDestination(
            url="https://public.example/webhook",
            host="public.example",
            port=443,
            addresses=((socket.AF_INET, "8.8.8.8"),),
        )

    @asynccontextmanager
    async def pinned_session(*_args, **_kwargs):
        class _Content:
            async def read(self, _limit):
                return b""

        class _Response:
            status = 204
            content = _Content()

        class _Client:
            @asynccontextmanager
            async def post(self, *_args, **_kwargs):
                yield _Response()

        yield _Client()

    monkeypatch.setattr(webhook_dispatcher, "resolve_webhook_destination", resolver)
    monkeypatch.setattr(webhook_dispatcher, "pinned_webhook_session", pinned_session)
    monkeypatch.setattr(webhook_dispatcher, "get_settings", lambda: SimpleNamespace())

    broken = await webhook_dispatcher.send_webhook(
        SimpleNamespace(url="https://broken.example/webhook", secret="s"),
        SimpleNamespace(id="one", payload={}, event_type="invoice.confirmed"),
    )
    delivered = await webhook_dispatcher.send_webhook(
        SimpleNamespace(url="https://public.example/webhook", secret="s"),
        SimpleNamespace(id="two", payload={}, event_type="invoice.confirmed"),
    )
    assert broken.error_code == "dns_resolution_failed"
    assert delivered.success
    assert calls == 2


async def test_dispatcher_total_deadline_covers_dns_and_http(monkeypatch):
    from src.workers import webhook_dispatcher

    async def stalled_resolver(*_args, **_kwargs):
        await asyncio.sleep(0.1)
        raise AssertionError("deadline should cancel DNS before this point")

    monkeypatch.setattr(webhook_dispatcher, "resolve_webhook_destination", stalled_resolver)
    result = await webhook_dispatcher.send_webhook(
        SimpleNamespace(url="https://merchant.example/webhook", secret="s"),
        SimpleNamespace(id="deadline", payload={}, event_type="invoice.confirmed"),
        timeout=0.01,
    )
    assert result.error_code == "timeout"



async def test_webhook_service_rejects_private_dns_before_storage(monkeypatch):
    from src.services.webhook_service import WebhookService

    async def blocked_resolver(*_args, **_kwargs):
        raise WebhookEgressError("webhook hostname resolves to a non-public address")

    session = SimpleNamespace(add=lambda _value: None)
    monkeypatch.setattr(
        "src.services.webhook_service.resolve_webhook_destination",
        blocked_resolver,
    )
    monkeypatch.setattr(
        "src.services.webhook_service.get_settings",
        lambda: SimpleNamespace(webhook_timeout_seconds=10),
    )
    with pytest.raises(ValueError, match="not permitted"):
        await WebhookService(session).create_webhook(
            merchant=SimpleNamespace(id="merchant-id"),
            url="https://merchant.example/webhook",
            events=["invoice.confirmed"],
        )


async def test_dispatcher_returns_non_sensitive_policy_block(monkeypatch):
    from src.workers.webhook_dispatcher import send_webhook

    async def blocked_resolver(*_args, **_kwargs):
        raise WebhookEgressError("private details must not leave the policy")

    monkeypatch.setattr(
        "src.workers.webhook_dispatcher.resolve_webhook_destination",
        blocked_resolver,
    )
    monkeypatch.setattr(
        "src.workers.webhook_dispatcher.get_settings",
        lambda: SimpleNamespace(webhook_internal_allowlist_entries=()),
    )
    outbox = SimpleNamespace(
        id="outbox-id",
        payload={"event": "invoice.confirmed"},
        event_type="invoice.confirmed",
    )
    result = await send_webhook(
        SimpleNamespace(url="https://legacy.example/webhook", secret="s"),
        outbox,
    )
    assert not result.success
    assert result.error_code == "egress_policy_blocked"


async def test_dispatcher_disables_redirects_and_caps_response_read(monkeypatch):
    from src.workers import webhook_dispatcher

    calls = {}

    class _Content:
        @staticmethod
        async def read(limit):
            calls["read_limit"] = limit

    class _Response:
        status = 204
        content = _Content()

    class _Client:
        @asynccontextmanager
        async def post(self, _url, **kwargs):
            calls.update(kwargs)
            yield _Response()

    destination = WebhookDestination(
        url="https://merchant.example/webhook",
        host="merchant.example",
        port=443,
        addresses=((socket.AF_INET, "8.8.8.8"),),
    )

    async def public_resolver(*_args, **_kwargs):
        return destination

    @asynccontextmanager
    async def pinned_session(*_args, **_kwargs):
        yield _Client()

    monkeypatch.setattr(webhook_dispatcher, "resolve_webhook_destination", public_resolver)
    monkeypatch.setattr(webhook_dispatcher, "pinned_webhook_session", pinned_session)
    monkeypatch.setattr(
        webhook_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(webhook_internal_allowlist_entries=()),
    )
    outbox = SimpleNamespace(
        id="outbox-id",
        payload={"event": "invoice.confirmed"},
        event_type="invoice.confirmed",
    )
    result = await webhook_dispatcher.send_webhook(
        SimpleNamespace(url=destination.url, secret="s"),
        outbox,
    )
    assert result.success
    assert calls["allow_redirects"] is False
    assert calls["read_limit"] == webhook_dispatcher._MAX_RESPONSE_BYTES


async def test_admin_merchant_onboarding_maps_concurrent_email_unique_error_to_409():
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    from src.api.admin.router import create_merchant

    class _ConcurrentSession:
        rolled_back = False

        def add(self, _value):
            return None

        async def flush(self):
            raise IntegrityError("INSERT merchants", {}, RuntimeError("duplicate"))

        async def rollback(self):
            self.rolled_back = True

    session = _ConcurrentSession()
    payload = MerchantCreateRequest(name="Merchant", email="merchant@example.test")
    with pytest.raises(HTTPException) as exc:
        await create_merchant(True, session, payload)
    assert exc.value.status_code == 409
    assert session.rolled_back


async def test_outbox_processing_counts_second_item_after_dns_failure(monkeypatch):
    from src.db.models import OutboxStatus
    from src.workers import webhook_dispatcher

    first = SimpleNamespace(
        id="first",
        webhook=SimpleNamespace(is_active=True),
        attempt=0,
        max_attempts=5,
        status=OutboxStatus.PENDING,
        next_retry_at=None,
        last_error=None,
        sent_at=None,
    )
    second = SimpleNamespace(
        id="second",
        webhook=SimpleNamespace(is_active=True),
        attempt=0,
        max_attempts=5,
        status=OutboxStatus.PENDING,
        next_retry_at=None,
        last_error=None,
        sent_at=None,
    )

    class _Scalars:
        def all(self):
            return [first, second]

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Session:
        committed = False

        async def execute(self, _statement):
            return _Result()

        async def commit(self):
            self.committed = True

    session = _Session()

    @asynccontextmanager
    async def session_context():
        yield session

    outcomes = iter(
        [
            webhook_dispatcher.WebhookDeliveryResult(False, "dns_resolution_failed"),
            webhook_dispatcher.WebhookDeliveryResult(True),
        ]
    )

    async def send(*_args, **_kwargs):
        return next(outcomes)

    monkeypatch.setattr(webhook_dispatcher, "get_session_context", session_context)
    monkeypatch.setattr(webhook_dispatcher, "send_webhook", send)
    monkeypatch.setattr(
        webhook_dispatcher,
        "get_settings",
        lambda: SimpleNamespace(webhook_timeout_seconds=1),
    )
    processed = await webhook_dispatcher.process_pending_webhooks()

    assert processed == 2
    assert session.committed
    assert first.attempt == 1
    assert first.last_error == "dns_resolution_failed"
    assert second.attempt == 1
    assert second.status == OutboxStatus.SENT



def _write_localhost_certificate(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "localhost-cert.pem"
    key_path = tmp_path / "localhost-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


@pytest.fixture
async def local_tls_webhook(tmp_path):
    certificate_path, key_path = _write_localhost_certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, key_path)
    client_context = ssl.create_default_context(cafile=certificate_path)
    received = {"redirect": 0, "ok": 0}

    async def ok(_request):
        received["ok"] += 1
        return web.Response(status=204)

    async def redirect(_request):
        received["redirect"] += 1
        raise web.HTTPFound("/ok")

    app = web.Application()
    app.router.add_post("/ok", ok)
    app.router.add_post("/redirect", redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield port, client_context, received
    finally:
        await runner.cleanup()


async def test_real_tls_pinned_transport_checks_sni_certificate_and_stops_redirect(
    local_tls_webhook,
):
    port, client_context, received = local_tls_webhook
    destination = WebhookDestination(
        url=f"https://localhost:{port}/redirect",
        host="localhost",
        port=port,
        addresses=((socket.AF_INET, "127.0.0.1"),),
    )
    async with pinned_webhook_session(
        destination,
        timeout_seconds=2,
        ssl_context=client_context,
    ) as client:
        async with client.post(destination.url, allow_redirects=False) as response:
            assert response.status == 302
    assert received == {"redirect": 1, "ok": 0}


async def test_real_tls_pinned_transport_rejects_certificate_hostname_mismatch(local_tls_webhook):
    port, client_context, _received = local_tls_webhook
    destination = WebhookDestination(
        url=f"https://wrong.example:{port}/ok",
        host="wrong.example",
        port=port,
        addresses=((socket.AF_INET, "127.0.0.1"),),
    )
    async with pinned_webhook_session(
        destination,
        timeout_seconds=2,
        ssl_context=client_context,
    ) as client:
        with pytest.raises(ClientConnectorCertificateError):
            await client.post(destination.url, allow_redirects=False)
