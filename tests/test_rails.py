"""
Тесты слоя рельсов: CryptoBot-адаптер, шифрование кредов, реестр.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("ENCRYPTION_KEY", "hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=")

from src.crypto.encryption import decrypt_secret, encrypt_secret  # noqa: E402
from src.payments.rails.base import (  # noqa: E402
    RailInvoiceRequest,
    RailInvoiceStatus,
    RailPayoutRequest,
    RailType,
)
from src.payments.rails.cryptobot import CryptobotRail  # noqa: E402
from src.payments.rails.registry import (  # noqa: E402
    build_rail,
    encrypt_credentials,
)


# === шифрование секретов ===


def test_secret_roundtrip():
    creds = {"token": "secret-token", "app": 12345}
    blob = encrypt_secret(json.dumps(creds), os.environ["ENCRYPTION_KEY"])
    assert decrypt_secret(blob, os.environ["ENCRYPTION_KEY"]) == json.dumps(creds)


def test_wrong_key_fails():
    blob = encrypt_secret("x", os.environ["ENCRYPTION_KEY"])
    with pytest.raises(ValueError):
        decrypt_secret(blob, "AQ" + "A" * 42)  # другой 32-байтный ключ


# === CryptoBot против фейкового API ===


@pytest.fixture
async def cryptobot_api():
    seen = []

    async def create_invoice(request):
        seen.append(
            {
                "token": request.headers.get("Crypto-Pay-API-Token"),
                "body": await request.json(),
            }
        )
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "invoice_id": 4242,
                    "status": "active",
                    "bot_invoice_url": "https://t.me/CryptoBot?start=I4242",
                    "asset": "USDT",
                    "amount": "10.5",
                },
            }
        )

    async def get_invoices(request):
        status = request.query.get("invoice_ids") and "paid"
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "items": [
                        {
                            "invoice_id": 4242,
                            "status": status or "active",
                            "bot_invoice_url": "https://t.me/CryptoBot?start=I4242",
                            "asset": "USDT",
                            "amount": "10.5",
                        }
                    ]
                },
            }
        )

    async def create_check(request):
        body = await request.json()
        seen.append({"check": body})
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "check_id": 99,
                    "bot_check_url": "https://t.me/CryptoBot?start=C99",
                },
            }
        )

    async def reject(request):
        return web.json_response(
            {"ok": False, "error": {"code": 401, "name": "AUTH_FAILED"}}, status=401
        )

    app = web.Application()
    app.router.add_post("/api/createInvoice", create_invoice)
    app.router.add_get("/api/getInvoices", get_invoices)
    app.router.add_post("/api/createCheck", create_check)
    app.router.add_post("/api/boom", reject)

    server = TestServer(app)
    await server.start_server()
    try:
        yield SimpleNamespace(url=str(server.make_url("/api")), seen=seen)
    finally:
        await server.close()


async def test_create_invoice_passes_token_and_payload(cryptobot_api):
    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        inv = await rail.create_invoice(
            RailInvoiceRequest(
                amount=Decimal("10.5"),
                asset="usdt",
                description="test",
                external_id="PAY_abc",
                expires_in_minutes=30,
            )
        )
    finally:
        await rail.close()

    assert inv.provider_invoice_id == "4242"
    assert inv.status == RailInvoiceStatus.PENDING
    assert inv.pay_url.startswith("https://t.me/CryptoBot")
    call = cryptobot_api.seen[0]
    assert call["token"] == "tok-1"
    assert call["body"]["asset"] == "USDT"
    assert call["body"]["expires_in"] == 1800
    assert call["body"]["payload"] == "PAY_abc"


async def test_verify_payment_returns_amount_when_paid(cryptobot_api):
    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        payment = await rail.verify_payment("4242")
    finally:
        await rail.close()
    assert payment is not None
    assert payment.amount == Decimal("10.5")
    assert payment.asset == "USDT"


async def test_payout_creates_check(cryptobot_api):
    rail = CryptobotRail("tok-1", base_url=cryptobot_api.url)
    try:
        payout = await rail.payout(
            RailPayoutRequest(amount=Decimal("5"), asset="USDT", destination="12345")
        )
    finally:
        await rail.close()
    assert payout.success
    assert payout.check_url.endswith("C99")
    assert cryptobot_api.seen[-1]["check"]["pin_to_user_id"] == 12345


async def test_api_error_is_permanent_for_auth(cryptobot_api):
    from src.payments.rails.base import RailError

    rail = CryptobotRail("bad", base_url=cryptobot_api.url)
    try:
        with pytest.raises(RailError) as exc:
            await rail._api("POST", "boom", {})
    finally:
        await rail.close()
    assert exc.value.permanent


def test_rail_requires_token():
    from src.payments.rails.base import RailError

    with pytest.raises(RailError):
        CryptobotRail("")


# === реестр ===


def test_registry_builds_cryptobot_with_decrypted_credentials(monkeypatch):
    from src.core.config import Settings
    from src.payments.rails import registry

    encrypted = encrypt_credentials({"token": "tok-9"})
    monkeypatch.setattr(
        registry,
        "get_settings",
        lambda: Settings(),
    )
    rail = build_rail("cryptobot", encrypted)
    assert isinstance(rail, CryptobotRail)
    assert rail.rail_type == RailType.CRYPTOBOT


def test_registry_rejects_unknown_rail():
    with pytest.raises(ValueError, match="unknown rail"):
        build_rail("p2p_cash", None)
