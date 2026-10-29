"""Server-driven Telegram checkout against the isolated gateway database and local Redis."""

import asyncio
import os
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from solders.keypair import Keypair
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from src.api.checkout.router import checkout_redis
from src.api.deps import get_current_merchant
from src.api.providers.router import provider_service
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.crypto.encryption import encrypt_secret
from src.db.models import DepositAddress, Invoice, PaymentSession
from src.main import create_app
from src.services.invoice_service import InvoiceService
from src.services.onchain_gateway_service import OnchainGatewayService
from src.services.telegram_checkout_service import (
    TelegramCheckoutService,
    surcharged,
)

pytest_plugins = ("tests.test_gateway_orchestration",)

USER = 7_000_001


@pytest_asyncio.fixture
async def checkout(gateway, monkeypatch):
    import redis.asyncio as aioredis

    url = os.getenv("LEDGER_API_COMPAT_REDIS_URL")
    if not url:
        pytest.skip("isolated Redis was not supplied")
    redis = aioredis.from_url(url, decode_responses=True)
    monkeypatch.setattr(limiter, "enabled", False)
    app = create_app()
    app.dependency_overrides[get_current_merchant] = lambda: gateway.merchant
    app.dependency_overrides[provider_service] = lambda: gateway.service
    app.dependency_overrides[checkout_redis] = lambda: redis
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as http:

        async def screen(action, user=USER, lang="en"):
            result = await http.post(
                "/v1/checkout/telegram",
                json={"telegram_user_id": user, "lang": lang, "action": action},
            )
            assert result.status_code == 200, result.text
            body = result.json()
            for row in body["buttons"]:
                for button in row:
                    if "callback_data" in button:
                        data = button["callback_data"]
                        assert data.startswith("ap:") and len(data.encode()) <= 64
            return body

        async def press(body, label, user=USER):
            data = next(
                b["callback_data"]
                for row in body["buttons"]
                for b in row
                if label in b["text"] and "callback_data" in b
            )
            return await screen({"type": "callback", "data": data}, user=user)

        yield type(
            "Checkout",
            (),
            {
                "http": http,
                "screen": staticmethod(screen),
                "press": staticmethod(press),
                "redis": redis,
                "service": TelegramCheckoutService(gateway.service, redis),
            },
        )
    await redis.aclose()


def profile(gateway, **overrides):
    value = {
        "product_title": "Shop <Pro>",
        "preset_amounts": ["10", "25"],
        "min_amount": "1",
        "max_amount": "500",
        "methods": [{"method": str(gateway.rail_id), "label": "xRocket", "surcharge_percent": "3"}],
    }
    value.update(overrides)
    return value


def enable_chain(gateway, monkeypatch):
    monkeypatch.setenv("NON_EVM_ENABLED", "true")
    monkeypatch.setenv("ONCHAIN_GATEWAY_ENABLED", "true")
    monkeypatch.setenv(
        "SOLANA_TREASURY_ADDRESS", str(Keypair.from_seed(uuid4().bytes * 2).pubkey())
    )
    get_settings.cache_clear()
    gateway.service.settings = get_settings()


def test_surcharge_never_short_changes_the_merchant():
    assert surcharged(Decimal("10"), Decimal("3")) == Decimal("10.30")
    assert surcharged(Decimal("9.99"), Decimal("2.5")) == Decimal("10.24")
    assert surcharged(Decimal("7.50"), Decimal("0")) == Decimal("7.50")


async def test_profile_validation(gateway, checkout, monkeypatch):
    http = checkout.http
    assert (await http.get("/v1/checkout/profile")).status_code == 404
    foreign = await gateway.service.connect(
        gateway.merchants[1].id, "xrocket", str(uuid4()), ["USDT"]
    )
    bad = [
        profile(gateway, methods=[{"method": str(foreign), "label": "x"}]),
        profile(gateway, methods=[{"method": "bsc", "label": "x"}]),
        profile(gateway, methods=[{"method": "solana", "label": "Solana"}]),
        profile(gateway, min_amount=1.5),
        profile(gateway, preset_amounts=["1000"]),
        profile(gateway, preset_amounts=["10.001"]),
        profile(gateway, preset_amounts=[], custom_amount=False),
        profile(
            gateway,
            methods=[{"method": str(gateway.rail_id), "label": "x", "surcharge_percent": "51"}],
        ),
    ]
    for body in bad:
        assert (await http.put("/v1/checkout/profile", json=body)).status_code == 422, body
    assert (await http.put("/v1/checkout/profile", json=profile(gateway))).status_code == 200
    stored = (await http.get("/v1/checkout/profile")).json()
    assert (
        stored["preset_amounts"] == ["10", "25"]
        and stored["methods"][0]["surcharge_percent"] == "3"
    )
    enable_chain(gateway, monkeypatch)
    chain = profile(gateway, methods=[{"method": "Solana", "label": "USDT · Solana"}])
    assert (await http.put("/v1/checkout/profile", json=chain)).status_code == 200
    assert (await http.get("/v1/checkout/profile")).json()["methods"][0]["method"] == "solana"


async def test_custodial_flow_double_press_paid_feed_and_ack(gateway, checkout):
    http = checkout.http
    assert (await http.put("/v1/checkout/profile", json=profile(gateway))).status_code == 200
    start = await checkout.screen({"type": "start"})
    assert "Shop &lt;Pro&gt;" in start["text"] and not start["expect_input"]
    assert [b["text"] for b in start["buttons"][0]] == ["$10", "$25"]
    methods = await checkout.press(start, "$10")
    assert "$10" in methods["text"] and "+3% fee" in methods["buttons"][0][0]["text"]
    # Another Telegram user pressing the same menu changes nothing.
    foreign = await checkout.press(methods, "xRocket", user=USER + 1)
    assert foreign["text"] is None and foreign["toast"] and gateway.state.posts == 0
    data = methods["buttons"][0][0]["callback_data"]
    screens = await asyncio.gather(
        *(checkout.screen({"type": "callback", "data": data}) for _ in range(3))
    )
    assert gateway.state.posts == 1
    invoice_screen = screens[0]
    assert all(s["text"] == invoice_screen["text"] for s in screens)
    assert invoice_screen["buttons"][0][0]["url"] == "https://t.me/local_test"
    assert "10.30 USDT" in invoice_screen["text"]
    async with gateway.factory() as session:
        invoice = await session.scalar(
            select(Invoice).where(Invoice.merchant_id == gateway.merchant.id)
        )
    assert invoice.amount == Decimal("10.30")
    assert invoice.extra_data == {
        "checkout": True,
        "telegram_user_id": USER,
        "requested_amount": "10.00",
        "surcharge_percent": "3",
    }
    assert (
        invoice.public_id in invoice_screen["text"]
        and str(invoice.id) not in invoice_screen["text"]
    )
    waiting = await checkout.press(invoice_screen, "Check payment")
    assert waiting["toast"] == "Not received yet" and not waiting["paid"]
    assert (await http.get("/v1/checkout/payments")).json() == {"items": []}
    assert (await http.get(f"/v1/checkout/payments/{invoice.id}")).status_code == 404
    gateway.state.paid, gateway.state.gross, gateway.state.net = True, "10.3", "10"
    await checkout.redis.delete(f"checkout:tg:{gateway.merchant.id}:poll:{invoice.id}")
    paid = await checkout.press(invoice_screen, "Check payment")
    assert paid["paid"] and "$10 will be credited" in paid["text"]
    feed = (await http.get("/v1/checkout/payments")).json()["items"]
    assert len(feed) == 1
    item = feed[0]
    assert item["invoice_id"] == str(invoice.id) and item["telegram_user_id"] == USER
    assert item["credit_amount"] == "10.00" and item["currency"] == "USD"
    assert item["external_id"] == f"gateway-{invoice.id}" and item["method"] == "xrocket"
    assert Decimal(item["gross"]) == Decimal("10.3") and not item["acked"]
    for _ in range(2):
        acked = await http.post(f"/v1/checkout/payments/{invoice.id}/ack")
        assert acked.status_code == 200 and acked.json()["acked"]
    assert (await http.get("/v1/checkout/payments")).json() == {"items": []}
    assert (await http.get(f"/v1/checkout/payments/{invoice.id}")).json()["acked"] is True
    # A replayed press after payment shows the paid screen, never a new invoice.
    again = await checkout.screen({"type": "callback", "data": data})
    assert again["paid"] and gateway.state.posts == 1
    async with gateway.factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("DELETE FROM checkout_payment_acks WHERE invoice_id=:id"), {"id": invoice.id}
            )
        await session.rollback()
    other = await http.post(f"/v1/checkout/payments/{uuid4()}/ack")
    assert other.status_code == 404


async def test_custom_amount_bounds_and_session_binding(gateway, checkout):
    assert (
        await checkout.http.put("/v1/checkout/profile", json=profile(gateway, max_amount="100"))
    ).status_code == 200
    stray = await checkout.screen({"type": "input", "text": "5"})
    assert "expired" in stray["text"] and not stray["expect_input"]
    start = await checkout.screen({"type": "start"}, lang="ru")
    assert "Выберите сумму" in start["text"]
    custom = await checkout.press(start, "Другая сумма")
    assert custom["expect_input"]
    for value in ("abc", "12.345", "-3", "1e3"):
        reply = await checkout.screen({"type": "input", "text": value}, lang="ru")
        assert reply["expect_input"] and "Это не сумма" in reply["text"]
    for value in ("0.99", "100.01"):
        reply = await checkout.screen({"type": "input", "text": value}, lang="ru")
        assert reply["expect_input"] and "Сумма должна быть от $1 до $100" in reply["toast"]
    assert (await checkout.screen({"type": "input", "text": "12"}, user=USER + 1))["text"]
    chosen = await checkout.screen({"type": "input", "text": "$12,5"}, lang="ru")
    assert not chosen["expect_input"] and "$12.50" in chosen["text"]
    # The pointer is cleared once a valid amount is chosen.
    late = await checkout.screen({"type": "input", "text": "13"})
    assert not late["expect_input"] and "$12.50" not in late["text"]
    cancelled = await checkout.press(chosen, "Отмена")
    assert "cancelled" in cancelled["text"]
    stale = await checkout.screen(
        {"type": "callback", "data": start["buttons"][0][0]["callback_data"]}
    )
    assert "expired" in stale["text"]
    unknown = await checkout.screen({"type": "callback", "data": "ap:nope:zz"})
    assert unknown["text"] is None and unknown["toast"]
    fresh = await checkout.screen({"type": "callback", "data": "ap:-:n"})
    assert fresh["buttons"][0][0]["text"] == "$10"


async def test_onchain_flow_underpay_then_overpay_credits_floor(gateway, checkout, monkeypatch):
    from tests.test_onchain_gateway import transfer

    enable_chain(gateway, monkeypatch)
    body = profile(gateway, methods=[{"method": "solana", "label": "USDT · Solana"}])
    assert (await checkout.http.put("/v1/checkout/profile", json=body)).status_code == 200
    address = str(Keypair.from_seed(uuid4().bytes * 2).pubkey())

    async def stub_selection(self, merchant_id, invoice_id, chain):
        async with gateway.factory() as session:
            invoice = await session.get(Invoice, invoice_id)
            deposit = DepositAddress(
                address=address,
                encrypted_privkey=encrypt_secret(
                    "unit-key", get_settings().encryption_key.get_secret_value()
                ),
                chain_group="solana",
                derivation_path="m/44'/501'/124'/0'",
                derivation_index=uuid4().int % 2**31,
            )
            session.add(deposit)
            await session.flush()
            session.add(
                PaymentSession(
                    invoice_id=invoice.id,
                    chain=chain,
                    token="USDT",
                    deposit_address_id=deposit.id,
                    expires_at=invoice.expires_at,
                )
            )
            await session.commit()

    monkeypatch.setattr(TelegramCheckoutService, "_select_onchain", stub_selection)
    start = await checkout.screen({"type": "start"})
    methods = await checkout.press(start, "$10")
    invoice_screen = await checkout.press(methods, "Solana")
    assert f"<code>{address}</code>" in invoice_screen["text"]
    assert (
        "<code>10.00</code> USDT" in invoice_screen["text"] and "Solana" in invoice_screen["text"]
    )
    assert not any("url" in b for row in invoice_screen["buttons"] for b in row)
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(
            transfer(address, "5", "checkout-part", symbol="USDT")
        )
    partial = await checkout.press(invoice_screen, "Check payment")
    assert "Received 5.00 of 10.00 USDT" in partial["text"] and not partial["paid"]
    assert (await checkout.http.get("/v1/checkout/payments")).json() == {"items": []}
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(
            transfer(address, "5.019", "checkout-rest", symbol="USDT")
        )
    paid = await checkout.press(invoice_screen, "Check payment")
    assert paid["paid"] and "$10.01" in paid["text"]
    [item] = (await checkout.http.get("/v1/checkout/payments")).json()["items"]
    assert item["credit_amount"] == "10.01" and item["method"] == "solana"
    assert Decimal(item["gross"]) == Decimal("10.019")


async def test_legacy_product_invoices_are_in_the_feed(gateway, checkout):
    async def paid_invoice(metadata):
        async with gateway.factory() as session:
            invoice = await InvoiceService(session).create_invoice(
                gateway.merchant,
                Decimal("7"),
                "USDT",
                [],
                allowed_rails=[gateway.rail_id],
                idempotency_key=str(uuid4()),
                metadata=metadata,
                customer_telegram_id="42",
            )
        operation = await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
        gateway.state.paid, gateway.state.gross, gateway.state.net = True, "7", "6.8"
        assert (
            await gateway.service.poll_invoice(gateway.merchant.id, operation.id)
        ).status == "completed"
        gateway.state.paid = False
        return invoice

    legacy = await paid_invoice(
        {"product": "llmass-topup", "telegram_user_id": 42, "external_user_id": "42"}
    )
    await paid_invoice({"order": "not a bot top-up", "telegram_user_id": 43})
    await paid_invoice({"product": "llmass-topup", "telegram_user_id": "44"})
    # A checkout claim above what the customer paid is never credited.
    await paid_invoice({"checkout": True, "telegram_user_id": 45, "requested_amount": "100"})
    items = (await checkout.http.get("/v1/checkout/payments?limit=10")).json()["items"]
    assert [(i["invoice_id"], i["telegram_user_id"], i["credit_amount"]) for i in items] == [
        (str(legacy.id), 42, "7.00")
    ]
    assert items[0]["external_id"] == f"gateway-{legacy.id}"
    assert UUID(items[0]["invoice_id"]) == legacy.id


async def test_sdk_connector_end_to_end_over_http(gateway, checkout):
    import socket
    import sys
    from pathlib import Path

    import uvicorn

    from src.core.security import hash_api_key
    from src.db.models import ApiKey
    from src.db.session import get_session

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk/python"))
    from arbitron_sdk import ArbitronClient, TelegramCheckout

    api_key = "checkout-isolated-" + uuid4().hex
    async with gateway.factory() as session, session.begin():
        session.add(
            ApiKey(
                merchant_id=gateway.merchant.id,
                key_hash=hash_api_key(api_key),
                key_prefix=api_key[:16],
                name="isolated checkout",
            )
        )

    async def sessions():
        async with gateway.factory() as session:
            yield session
            await session.commit()

    app = create_app()
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[provider_service] = lambda: gateway.service
    app.dependency_overrides[checkout_redis] = lambda: checkout.redis
    credited = []

    async def credit(user_id, amount, external_id, item):
        credited.append((user_id, amount, external_id))
        return True

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with ArbitronClient(endpoint, api_key) as client:
                await client.put_checkout_profile(profile(gateway))
                connector = TelegramCheckout(client, credit)
                screen = await connector.start(USER, "en")
                screen = await connector.callback(USER, screen.buttons[0][1]["callback_data"])
                assert "$25" in screen.text
                screen = await connector.callback(USER, screen.buttons[0][0]["callback_data"])
                assert screen.buttons[0][0]["url"] == "https://t.me/local_test"
                check = screen.buttons[1][0]["callback_data"]
                assert connector.is_ours(check)
                gateway.state.paid, gateway.state.gross, gateway.state.net = True, "25.75", "25"
                paid = await connector.callback(USER, check)
                assert paid.paid and len(credited) == 1
                assert credited[0][:2] == (USER, Decimal("25.00"))
                assert await connector.sync() == 0
                assert await client.checkout_payments() == []
        finally:
            server.should_exit = True
            await task
