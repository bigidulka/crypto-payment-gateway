"""
Тесты SDK.

Подпись вебхука проверяется против серверной реализации из src/core/security -
это контракт, и SDK обязан совпадать с ним байт в байт.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestServer

SDK = Path(__file__).resolve().parents[1]
GATEWAY = SDK.parents[1]
sys.path.insert(0, str(SDK))
sys.path.insert(0, str(GATEWAY))

from arbitron_sdk import (  # noqa: E402
    ArbitronClient,
    ArbitronError,
    InvoiceStatus,
    WebhookVerificationError,
    verify_webhook,
)
from arbitron_sdk.webhooks import compute_signature  # noqa: E402


# === Вебхуки: контракт с сервером ===


def _server_signature(payload: bytes, secret: str, timestamp: int) -> str:
    import os

    os.environ.setdefault("SECRET_KEY", "x" * 40)
    os.environ.setdefault("ENCRYPTION_KEY", "x" * 40)
    from src.core.security import generate_hmac_signature

    signature, _ = generate_hmac_signature(payload, secret, timestamp)
    return signature


def test_signature_matches_server_implementation():
    payload = json.dumps({"event": "invoice.confirmed", "amount": "10"}).encode()
    assert compute_signature(payload, "s3cret", 1_700_000_000) == _server_signature(
        payload, "s3cret", 1_700_000_000
    )


def _headers(payload: bytes, secret: str, ts: int, event: str = "invoice.confirmed"):
    return {
        "X-Webhook-Signature": compute_signature(payload, secret, ts),
        "X-Webhook-Timestamp": str(ts),
        "X-Webhook-Event": event,
    }


def test_valid_webhook_is_parsed():
    payload = b'{"event":"invoice.confirmed","public_id":"PAY_1","amount":"10"}'
    event = verify_webhook(payload, _headers(payload, "s", 1000), "s", now=1000)
    assert event.event_type == "invoice.confirmed"
    assert event.data["public_id"] == "PAY_1"
    assert event.timestamp == 1000


def test_headers_are_case_insensitive():
    payload = b'{"event":"x"}'
    headers = {k.lower(): v for k, v in _headers(payload, "s", 1000, event="x").items()}
    assert verify_webhook(payload, headers, "s", now=1000).event_type == "x"


def test_tampered_body_is_rejected():
    payload = b'{"amount":"10"}'
    headers = _headers(payload, "s", 1000)
    with pytest.raises(WebhookVerificationError, match="mismatch"):
        verify_webhook(b'{"amount":"1000"}', headers, "s", now=1000)


def test_wrong_secret_is_rejected():
    payload = b'{"a":1}'
    with pytest.raises(WebhookVerificationError, match="mismatch"):
        verify_webhook(payload, _headers(payload, "s", 1000), "other", now=1000)


def test_stale_signature_is_rejected():
    payload = b'{"a":1}'
    with pytest.raises(WebhookVerificationError, match="too old"):
        verify_webhook(payload, _headers(payload, "s", 1000), "s", now=1000 + 301)


def test_missing_headers_are_rejected():
    with pytest.raises(WebhookVerificationError, match="missing"):
        verify_webhook(b"{}", {}, "s")


def test_reformatted_json_does_not_verify():
    """Подпись над байтами: пересериализованный JSON её ломает."""
    original = b'{"a": 1, "b": 2}'
    headers = _headers(original, "s", 1000)
    reformatted = json.dumps(json.loads(original), separators=(",", ":")).encode()
    assert reformatted != original
    with pytest.raises(WebhookVerificationError):
        verify_webhook(reformatted, headers, "s", now=1000)


# === Клиент: против фейкового шлюза ===


@pytest.fixture
async def gateway():
    calls = []

    async def chains(request):
        return web.json_response(
            {
                "chains": {
                    "bsc": {
                        "name": "BNB Chain",
                        "tokens": [{"symbol": "USDT"}, {"symbol": "USDC"}],
                        "is_active": True,
                        "estimated_credit_seconds": 11.8,
                    },
                    "dead": {"name": "Dead", "tokens": [], "is_active": False},
                }
            }
        )

    async def create_invoice(request):
        calls.append(
            {
                "auth": request.headers.get("Authorization"),
                "idem": request.headers.get("Idempotency-Key"),
                "body": await request.json(),
            }
        )
        return web.json_response(
            {
                "id": "inv-1",
                "public_id": "PAY_abc",
                "hosted_url": "https://pay/PAY_abc",
                "status": "AWAITING_PAYMENT",
                "amount": "10",
                "asset": "USDT",
                "allowed_chains": ["bsc"],
                "expires_at": "2026-01-01T00:00:00Z",
            }
        )

    async def select(request):
        body = await request.json()
        return web.json_response(
            {
                "deposit_address": "0xdead",
                "amount": "10",
                "chain": body["chain"],
                "token": body["token"],
                "chain_name": "BNB Chain",
                "qr_data": "ethereum:0xdead",
            }
        )

    async def status(request):
        return web.json_response(
            {
                "status": "AWAITING_PAYMENT",
                "amount": "10",
                "asset": "USDT",
                "is_expired": False,
                "received_amount": "8",
                "mismatch_reason": "underpaid",
                "missing_amount": "2",
            }
        )

    async def boom(request):
        return web.json_response({"detail": "nope"}, status=403)

    app = web.Application()
    app.router.add_get("/v1/public/chains", chains)
    app.router.add_post("/v1/invoices", create_invoice)
    app.router.add_post("/pay/{pid}/select", select)
    app.router.add_get("/pay/{pid}/status", status)
    app.router.add_get("/v1/invoices/forbidden", boom)
    server = TestServer(app)
    await server.start_server()
    try:
        yield SimpleNamespace(url=str(server.make_url("")), calls=calls)
    finally:
        await server.close()


async def test_create_invoice_sends_key_and_idempotency(gateway):
    async with ArbitronClient(gateway.url, "key-1") as c:
        inv = await c.create_invoice(
            amount=Decimal("10"),
            asset="usdt",
            allowed_chains=["bsc"],
            external_user_id="shop:42",
            idempotency_key="order-7",
        )
    assert inv.public_id == "PAY_abc"
    assert inv.status == InvoiceStatus.AWAITING_PAYMENT
    call = gateway.calls[0]
    assert call["auth"] == "Bearer key-1"
    assert call["idem"] == "order-7"
    assert call["body"]["asset"] == "USDT"
    assert call["body"]["metadata"]["external_user_id"] == "shop:42"


async def test_chains_filters_inactive_and_caches(gateway):
    async with ArbitronClient(gateway.url, "k") as c:
        chains = await c.get_chains()
        assert set(chains) == {"bsc"}
        assert chains["bsc"].tokens == ("USDT", "USDC")
        assert chains["bsc"].estimated_credit_seconds == 11.8
        assert await c.get_chains() is chains  # из кэша


async def test_select_and_status(gateway):
    async with ArbitronClient(gateway.url, "k") as c:
        sel = await c.select_payment("PAY_abc", chain="bsc", token="usdt")
        assert sel.deposit_address == "0xdead"
        assert sel.token == "USDT"

        st = await c.get_payment_status("PAY_abc")
        assert not st.is_paid
        assert st.mismatch_reason == "underpaid"
        assert st.missing_amount == Decimal("2")


async def test_http_error_becomes_arbitron_error(gateway):
    async with ArbitronClient(gateway.url, "k") as c:
        with pytest.raises(ArbitronError) as exc:
            await c.get_invoice("forbidden")
    assert exc.value.status == 403
    assert "nope" in exc.value.body


def test_client_rejects_missing_credentials():
    with pytest.raises(ValueError):
        ArbitronClient("", "k")
    with pytest.raises(ValueError):
        ArbitronClient("http://x", "")
