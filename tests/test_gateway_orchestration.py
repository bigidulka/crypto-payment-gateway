"""Isolated PostgreSQL/runtime-role and local HTTP financial acceptance paths.

These tests never call real providers and never reset existing databases.
"""

import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import get_settings
from src.core.exceptions import ValidationError
from src.db.models import (
    GatewayPayoutReceipt,
    Invoice,
    InvoiceEvent,
    LedgerEntry,
    Merchant,
    OutboxWebhook,
    ProviderPaymentMapping,
    ProviderPaymentObservation,
    ProviderPaymentReceipt,
    ProviderSettlement,
    Webhook,
)
from src.payments.rails.base import RailPayment
from src.payments.rails.xrocket import XRocketRail
from src.services.gateway_provider_service import GatewayError, GatewayProviderService
from src.services.invoice_service import InvoiceService
from src.services.provider_payment_ingestion import (
    ProviderPaymentIngestionService,
    ProviderVerificationBinding,
)


@pytest_asyncio.fixture
async def gateway(monkeypatch):
    value = os.getenv("GATEWAY_TEST_RUNTIME_DATABASE_URL")
    if not value:
        pytest.skip("explicit isolated gateway runtime database was not supplied")
    url = make_url(value)
    assert url.host in {"127.0.0.1", "::1"} and url.database.startswith("test_gateway_")
    assert url.drivername == "postgresql+asyncpg"
    engine = create_async_engine(value)
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT version_num FROM alembic_version"))
            == "0016_liquidity_recognition"
        )
        assert await conn.scalar(text("SELECT current_database()")) == url.database
        role = (
            await conn.execute(
                text(
                    "SELECT rolsuper,rolcreaterole,rolcreatedb,rolbypassrls "
                    "FROM pg_roles WHERE rolname=current_user"
                )
            )
        ).one()
        assert role == (False, False, False, False)
        assert not await conn.scalar(
            text("SELECT has_schema_privilege(current_user,'public','CREATE')")
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    merchants = [
        Merchant(id=uuid4(), name="gateway isolated", email=f"{uuid4()}@test.invalid")
        for _ in range(2)
    ]
    async with factory() as session, session.begin():
        session.add_all(merchants)
    monkeypatch.setenv("LEDGER_RAIL_ORCHESTRATION_ENABLED", "true")
    monkeypatch.setenv(
        "LEDGER_RAIL_ORCHESTRATION_MERCHANTS", ",".join(str(m.id) for m in merchants)
    )
    get_settings.cache_clear()
    state = SimpleNamespace(
        invoices={},
        posts=0,
        unknown=False,
        paid=False,
        gross="100",
        net="97",
        payouts={},
        payout_posts=0,
        payout_unknown=False,
        payout_status="finished",
        provider_balance=Decimal("500"),
    )
    app = web.Application()

    async def identity(request):
        return web.json_response({"id": request.headers["Authorization"]})

    async def create(request):
        body = await request.json()
        state.posts += 1
        response = {
            "id": str(uuid4()),
            "clientInvoiceId": body["clientInvoiceId"],
            "priceCurrency": body["priceCurrency"],
            "priceAmount": body["priceAmount"],
            "status": "active",
            "links": {"telegramBotLink": "https://t.me/local_test"},
        }
        state.invoices[body["clientInvoiceId"]] = response
        if state.unknown:
            return web.json_response({"detail": "unknown upstream result"}, status=503)
        return web.json_response(response)

    async def invoice(request):
        if "clientInvoiceId" in request.query:
            response = state.invoices[request.query["clientInvoiceId"]]
        else:
            response = next(
                row for row in state.invoices.values() if row["id"] == request.query["invoiceId"]
            )
        return web.json_response({**response, "status": "paid" if state.paid else "active"})

    async def payments(request):
        return web.json_response(
            {
                "items": [
                    {
                        "id": "p-" + request.query["invoiceId"],
                        "status": "paid",
                        "payCurrency": "USDT",
                        "receiveCurrency": "USDT",
                        "payAmount": state.gross,
                        "receiveAmount": state.net,
                    }
                ],
                "pagination": {"next": None},
            }
        )

    app.router.add_get("/api/v1/app-info", identity)
    app.router.add_post("/api/v1/invoices", create)
    app.router.add_get("/api/v1/invoice", invoice)
    app.router.add_get("/api/v1/invoice/payments", payments)

    async def balances(request):
        return web.json_response(
            {
                "balances": [
                    {
                        "asset": "USDT",
                        "balance": str(state.provider_balance),
                        "available": str(state.provider_balance),
                        "holds": "0",
                    }
                ]
            }
        )

    async def payout(request):
        body = await request.json()
        state.payout_posts += 1
        response = {**body, "payoutId": str(uuid4()), "status": state.payout_status}
        state.payouts[body["clientPayoutId"]] = response
        state.provider_balance -= Decimal(body["amount"])
        if state.payout_unknown:
            return web.json_response({"detail": "unknown upstream outcome"}, status=503)
        return web.json_response(response)

    async def get_payout(request):
        return web.json_response(state.payouts[request.query["clientPayoutId"]])

    app.router.add_get("/api/v1/balances", balances)
    app.router.add_post("/api/v1/payouts", payout)
    app.router.add_get("/api/v1/payout", get_payout)
    async with TestServer(app) as server:

        def builder(kind, credentials, **kwargs):
            # Credentials are only decrypted in the server-owned adapter builder.
            import json

            from src.crypto.encryption import decrypt_secret

            token = json.loads(
                decrypt_secret(
                    bytes.fromhex(credentials), get_settings().encryption_key.get_secret_value()
                )
            )["token"]
            assert kind == "xrocket"
            return XRocketRail(token, base_url=str(server.make_url("/")), **kwargs)

        service = GatewayProviderService(factory, rail_builder=builder)
        rail_id = await service.connect(merchants[0].id, "xrocket", str(uuid4()), ["USDT"])
        try:
            yield SimpleNamespace(
                factory=factory,
                service=service,
                state=state,
                merchants=merchants,
                merchant=merchants[0],
                rail_id=rail_id,
            )
        finally:
            get_settings.cache_clear()
            await engine.dispose()


async def new_invoice(gateway, *, amount=Decimal("100"), key=None):
    async with gateway.factory() as session:
        invoice = await InvoiceService(session).create_invoice(
            gateway.merchant,
            amount,
            "USDT",
            [],
            allowed_rails=[gateway.rail_id],
            idempotency_key=key,
            metadata={"order": "isolated"},
        )
    return invoice


@pytest.mark.asyncio
async def test_sdk_through_http_api_checkout_receipt_and_payout(gateway, monkeypatch, tmp_path):
    import json
    import socket
    import sys
    import time
    from pathlib import Path

    import httpx
    import uvicorn

    from src.api.providers.router import provider_service
    from src.core.rate_limit import limiter
    from src.core.security import hash_api_key
    from src.db.models import ApiKey
    from src.db.session import get_session
    from src.main import create_app

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk/python"))
    from arbitron_sdk import ArbitronClient, ArbitronError
    from arbitron_sdk.webhooks import WebhookVerificationError, compute_signature

    from examples.product_payment import ProductPayments

    api_key = "gateway-isolated-" + uuid4().hex
    async with gateway.factory() as session, session.begin():
        session.add(
            ApiKey(
                merchant_id=gateway.merchant.id,
                key_hash=hash_api_key(api_key),
                key_prefix=api_key[:16],
                name="isolated SDK",
            )
        )

    async def sessions():
        async with gateway.factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    monkeypatch.setattr(limiter, "enabled", False)
    app = create_app()
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[provider_service] = lambda: gateway.service
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
                methods = await client.payment_methods()
                assert [m["id"] for m in methods] == [str(gateway.rail_id)]
                invoice = await client.create_invoice(
                    amount="100",
                    asset="USDT",
                    allowed_chains=[],
                    allowed_rails=[str(gateway.rail_id)],
                    external_user_id="shop:user1",
                    idempotency_key=str(uuid4()),
                )
                assert invoice.allowed_rails == (str(gateway.rail_id),)
                assert (
                    await client.get_invoice(invoice.invoice_id)
                ).allowed_rails == invoice.allowed_rails
                assert (await client.invoice_methods(invoice.public_id))["items"][0][
                    "type"
                ] == "xrocket"
                selected = await client.select_invoice_provider(
                    invoice.invoice_id, str(gateway.rail_id)
                )
                assert (
                    selected.pay_url == "https://t.me/local_test" and selected.status == "pending"
                )
                replay = await client.select_provider(invoice.public_id, str(gateway.rail_id))
                assert replay.id == selected.id and gateway.state.posts == 1
                async with ArbitronClient(endpoint, "invalid") as outsider:
                    with pytest.raises(ArbitronError) as error:
                        await outsider.select_invoice_provider(
                            invoice.invoice_id, str(gateway.rail_id)
                        )
                    assert error.value.status == 401
                assert (await client.invoice_receipt(invoice.invoice_id))["receipt"] is None
                gateway.state.paid = True
                await gateway.service.poll_batch()
                receipt = (await client.invoice_receipt(invoice.invoice_id))["receipt"]
                assert receipt["gross"] == "100.00000000" and receipt["net"] == "97.00000000"
                assert receipt["provider_fee"] == "3.00000000"
                gateway.state.provider_balance = Decimal("15.4812")
                balance = (await client.provider_balances(str(gateway.rail_id)))[0]
                assert (
                    balance.available == Decimal("15.48120000")
                    and balance.availability == "verified"
                )
                assert balance.unledgered_available == 0
                payout = await client.payout(
                    rail_id=str(gateway.rail_id),
                    amount="15.4812",
                    asset="USDT",
                    destination="123",
                    idempotency_key=str(uuid4()),
                )
                assert payout.status == "completed"
                assert (await client.get_operation(payout.id)).status == "completed"
                async with ArbitronClient(endpoint, "invalid") as outsider:
                    with pytest.raises(ArbitronError) as error:
                        await outsider.get_operation(payout.id)
                    assert error.value.status == 401
                async with httpx.AsyncClient(base_url=endpoint) as browser:
                    public = (await browser.get(f"/pay/{invoice.public_id}/methods")).json()
                    assert "ledger_asset_id" not in str(
                        public
                    ) and "customer_telegram_id" not in str(public)
                    # A caller supplied PAID fact is never a verification path.
                    forged = await browser.post(
                        f"/pay/{invoice.public_id}/select-provider",
                        json={"rail_id": str(gateway.rail_id), "paid": True, "amount": "100"},
                    )
                    assert forged.status_code == 422
                secret = "product-webhook-test-secret-" + uuid4().hex
                async with gateway.factory() as session, session.begin():
                    session.add(
                        Webhook(
                            merchant_id=gateway.merchant.id,
                            url="https://product.test/hooks/arbitron",
                            secret=secret,
                            events=["invoice.confirmed"],
                        )
                    )
                database = tmp_path / "orders.sqlite3"
                product = ProductPayments(client, database, secret)
                order_id = uuid4()
                gateway.state.paid = False
                purchase = await product.create_order(
                    order_id, "customer:42", Decimal("100"), "USDT", str(gateway.rail_id)
                )
                # The product issues a hosted checkout; the customer selects the
                # provider there. Products never need the provider token or SDK.
                await client.select_provider(purchase["public_id"], str(gateway.rail_id))
                gateway.state.paid = True
                await gateway.service.poll_batch()
                async with gateway.factory() as session:
                    outbox = await session.scalar(
                        select(OutboxWebhook).where(
                            OutboxWebhook.invoice_id == purchase["invoice_id"],
                            OutboxWebhook.event_type == "invoice.confirmed",
                        )
                    )
                    body = json.dumps(outbox.payload, separators=(",", ":")).encode()
                timestamp = int(time.time())
                headers = {
                    "X-Webhook-Timestamp": str(timestamp),
                    "X-Webhook-Event": "invoice.confirmed",
                    "X-Webhook-Signature": compute_signature(body, secret, timestamp),
                }
                assert await product.receive_webhook(body, headers)
                restarted = ProductPayments(client, database, secret)
                assert not await restarted.receive_webhook(body, headers)
                assert (
                    await restarted.create_order(
                        order_id, "customer:42", Decimal("100"), "USDT", str(gateway.rail_id)
                    )
                )["status"] == "CONFIRMED"
                with pytest.raises(ValueError, match="another purchase"):
                    await restarted.create_order(
                        order_id, "customer:42", Decimal("101"), "USDT", str(gateway.rail_id)
                    )
                with pytest.raises(WebhookVerificationError):
                    await restarted.receive_webhook(body + b" ", headers)
        finally:
            server.should_exit = True
            await task


@pytest.mark.asyncio
async def test_select_replay_and_parallel_selection_never_create_two_invoices(gateway):
    invoice = await new_invoice(gateway)
    results = await asyncio.gather(
        *(gateway.service.select_invoice(invoice.public_id, gateway.rail_id) for _ in range(3))
    )
    assert len({r.id for r in results}) == 1
    assert gateway.state.posts == 1
    assert (
        await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    ).status == "pending"
    methods = await gateway.service.list_methods(gateway.merchant.id)
    assert (
        len(methods) == 1
        and "encrypted_credentials" not in methods[0]
        and "token" not in str(methods)
    )
    with pytest.raises(GatewayError):
        await gateway.service.get_operation(gateway.merchants[1].id, results[0].id)


@pytest.mark.asyncio
async def test_unknown_creation_recovers_by_client_id_without_resubmitting(gateway):
    gateway.state.unknown = True
    invoice = await new_invoice(gateway)
    operation = await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    assert operation.status == "unknown" and gateway.state.posts == 1
    assert (
        await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    ).status == "unknown"
    recovered = await gateway.service.poll_invoice(gateway.merchant.id, operation.id)
    assert recovered.status == "pending" and recovered.provider_reference
    assert gateway.state.posts == 1


@pytest.mark.asyncio
async def test_net_credit_receipt_and_webhook_commit_once_under_parallel_polling(gateway):
    async with gateway.factory() as session, session.begin():
        session.add(
            Webhook(
                merchant_id=gateway.merchant.id,
                url="https://example.test/webhook",
                secret="x" * 32,
                events=["invoice.confirmed"],
            )
        )
    invoice = await new_invoice(gateway)
    op = await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    gateway.state.paid = True
    replies = await asyncio.gather(
        *(gateway.service.poll_invoice(gateway.merchant.id, op.id) for _ in range(3))
    )
    assert all(r.status == "completed" for r in replies)
    async with gateway.factory() as session:
        assert (await session.get(Invoice, invoice.id)).status.value == "CONFIRMED"
        facts = (
            await session.scalars(
                select(ProviderPaymentObservation).where(
                    ProviderPaymentObservation.merchant_id == gateway.merchant.id
                )
            )
        ).all()
        assert len(facts) == 1 and int(facts[0].observed_amount_atomic) == 10000000000
        net = await session.get(ProviderSettlement, facts[0].id)
        assert int(net.settled_amount_atomic) == 9700000000
        entries = (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.merchant_id == gateway.merchant.id)
            )
        ).all()
        assert len(entries) == 2 and {int(e.amount_atomic) for e in entries} == {9700000000}
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ProviderPaymentReceipt)
                .where(ProviderPaymentReceipt.merchant_id == gateway.merchant.id)
            )
            == 1
        )
        events = (
            await session.scalars(
                select(InvoiceEvent).where(
                    InvoiceEvent.invoice_id == invoice.id,
                    InvoiceEvent.event_type == "invoice.confirmed",
                )
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload["observed"]["settled_amount_atomic"] == "9700000000"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxWebhook)
                .where(
                    OutboxWebhook.invoice_id == invoice.id,
                    OutboxWebhook.event_type == "invoice.confirmed",
                )
            )
            == 1
        )
    async with gateway.factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE provider_settlements SET settled_amount_atomic="
                    "settled_amount_atomic+1 WHERE observation_id=:id"
                ),
                {"id": facts[0].id},
            )
        await session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("gross,net", [("99", "97"), ("101", "97"), ("100", None)])
async def test_conflicting_or_unknown_settlement_never_credits_requested_amount(
    gateway, gross, net
):
    invoice = await new_invoice(gateway)
    op = await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    gateway.state.paid, gateway.state.gross, gateway.state.net = True, gross, net
    result = await gateway.service.poll_invoice(gateway.merchant.id, op.id)
    assert result.status in {"pending", "reconciliation_required"}
    async with gateway.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.merchant_id == gateway.merchant.id)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_invoice_idempotency_key_binds_full_request(gateway):
    key = str(uuid4())
    invoice = await new_invoice(gateway, key=key)
    replay = await new_invoice(gateway, amount=Decimal("100.00"), key=key)
    assert replay.id == invoice.id and replay.extra_data == {"order": "isolated"}
    with pytest.raises(ValidationError):
        await new_invoice(gateway, amount=Decimal("101"), key=key)


@pytest.mark.asyncio
async def test_legacy_gross_receipt_cannot_receive_late_net_fact(gateway):
    invoice = await new_invoice(gateway)
    await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    async with gateway.factory() as session:
        mapping = await session.scalar(
            select(ProviderPaymentMapping).where(ProviderPaymentMapping.invoice_id == invoice.id)
        )
        binding = ProviderVerificationBinding(
            gateway.merchant.id,
            mapping.id,
            mapping.rail_id,
            mapping.provider_account_identity,
            mapping.provider_network,
            mapping.ledger_asset_id,
        )
        verifier = SimpleNamespace(
            binding=binding,
            verify_payment=AsyncMock(
                return_value=RailPayment(
                    mapping.provider_invoice_id, Decimal("100"), "USDT", raw={"status": "paid"}
                )
            ),
        )
        await session.rollback()
        service = ProviderPaymentIngestionService(session)
        fact = await service.verify_provider_invoice(
            gateway.merchant.id, binding.mapping_id, verifier
        )
        assert fact.settled_amount_atomic is None
        async with session.begin():
            observation = await service.consume_verified_observation(gateway.merchant.id, fact)
            observation_id = observation.id
    async with gateway.factory() as session:
        with pytest.raises(DBAPIError, match="settlement cannot change an already proven receipt"):
            session.add(
                ProviderSettlement(
                    observation_id=observation_id,
                    merchant_id=gateway.merchant.id,
                    settled_amount_atomic=1,
                )
            )
            await session.flush()
        await session.rollback()
        assert await session.get(ProviderSettlement, observation_id) is None
        receipt = await session.scalar(
            select(ProviderPaymentReceipt).where(
                ProviderPaymentReceipt.observation_id == observation_id
            )
        )
        entries = (
            await session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.ledger_transaction_id == receipt.ledger_transaction_id
                )
            )
        ).all()
        assert len(entries) == 2 and {int(entry.amount_atomic) for entry in entries} == {
            10000000000
        }


@pytest.mark.asyncio
async def test_database_refuses_forged_completion_and_connection_rebinding(gateway):
    invoice = await new_invoice(gateway)
    operation = await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    async with gateway.factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE gateway_operations SET status='completed' WHERE id=:id"),
                {"id": operation.id},
            )
        await session.rollback()


async def credit(gateway):
    invoice = await new_invoice(gateway)
    operation = await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    gateway.state.paid = True
    assert (
        await gateway.service.poll_invoice(gateway.merchant.id, operation.id)
    ).status == "completed"


@pytest.mark.asyncio
async def test_balance_caps_ledger_credit_to_actual_provider_availability(gateway):
    await credit(gateway)
    gateway.state.provider_balance = Decimal("15.4812")
    balance = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert balance["ledger_balance"] == Decimal("97")
    assert balance["available"] == Decimal("15.4812")
    assert balance["availability"] == "verified"
    with pytest.raises(GatewayError, match="Insufficient"):
        await gateway.service.create_payout(
            gateway.merchant.id, gateway.rail_id, Decimal("16"), "USDT", "123", str(uuid4())
        )
    assert gateway.state.payout_posts == 0


@pytest.mark.asyncio
async def test_completed_payout_debits_exact_net_balance_once(gateway):
    await credit(gateway)
    key = str(uuid4())
    operation = await gateway.service.create_payout(
        gateway.merchant.id, gateway.rail_id, Decimal("60"), "USDT", "123", key
    )
    assert operation.status == "completed"
    replay = await gateway.service.create_payout(
        gateway.merchant.id, gateway.rail_id, Decimal("60.00"), "USDT", "123", key
    )
    assert replay.id == operation.id and gateway.state.payout_posts == 1
    balance = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert balance["available"] == balance["ledger_balance"] == Decimal("37")
    assert balance["reserved"] == 0
    async with gateway.factory() as session:
        assert await session.get(GatewayPayoutReceipt, operation.id)
        entries = (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.merchant_id == gateway.merchant.id)
            )
        ).all()
        assert sorted(int(e.amount_atomic) for e in entries) == [
            6000000000,
            6000000000,
            9700000000,
            9700000000,
        ]
    with pytest.raises(GatewayError, match="another payout"):
        await gateway.service.create_payout(
            gateway.merchant.id, gateway.rail_id, Decimal("61"), "USDT", "123", key
        )


@pytest.mark.asyncio
async def test_parallel_payouts_unknown_outcome_and_restart_recovery_preserve_reserve(gateway):
    await credit(gateway)
    gateway.state.payout_unknown = True
    results = await asyncio.gather(
        *(
            gateway.service.create_payout(
                gateway.merchant.id, gateway.rail_id, Decimal("60"), "USDT", "123", str(uuid4())
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    operations = [r for r in results if not isinstance(r, BaseException)]
    assert len(operations) == 1 and operations[0].status == "unknown"
    assert any(isinstance(r, GatewayError) and r.code == "INSUFFICIENT_FUNDS" for r in results)
    assert gateway.state.payout_posts == 1
    before = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert before["reserved"] == Decimal("60") and before["ledger_balance"] == Decimal("97")
    # A fresh service instance has no local operation cache and only reads back.
    fresh = GatewayProviderService(gateway.factory, rail_builder=gateway.service.rail_builder)
    result = await fresh.reconcile_payout(gateway.merchant.id, operations[0].id)
    assert result.status == "completed" and gateway.state.payout_posts == 1
    after = (await fresh.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert after["available"] == Decimal("37") and after["reserved"] == 0


@pytest.mark.asyncio
async def test_pending_payout_receipt_must_keep_original_recipient(gateway):
    await credit(gateway)
    gateway.state.payout_status = "pending"
    operation = await gateway.service.create_payout(
        gateway.merchant.id, gateway.rail_id, Decimal("60"), "USDT", "123", str(uuid4())
    )
    assert operation.status == "pending"
    record = gateway.state.payouts[str(operation.id)]
    record.update(status="finished", target="999")
    assert (
        await gateway.service.reconcile_payout(gateway.merchant.id, operation.id)
    ).status == "reconciliation_required"
    balance = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert balance["reserved"] == Decimal("60")
    async with gateway.factory() as session:
        assert await session.get(GatewayPayoutReceipt, operation.id) is None
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE gateway_rail_connections SET account_identity=:identity "
                    "WHERE rail_id=:id"
                ),
                {"identity": "f" * 64, "id": gateway.rail_id},
            )
        await session.rollback()


async def recognitions(gateway):
    from src.db.models import GatewayLiquidityRecognition

    async with gateway.factory() as session:
        return (
            await session.scalars(
                select(GatewayLiquidityRecognition).where(
                    GatewayLiquidityRecognition.merchant_id == gateway.merchant.id
                )
            )
        ).all()


@pytest.mark.asyncio
async def test_pre_gateway_funds_require_explicit_recognition_bound_to_payout(gateway):
    # Funds already held by the sole-owner account before any gateway fact.
    gateway.state.provider_balance = Decimal("500")
    with pytest.raises(GatewayError, match="Insufficient"):
        await gateway.service.create_payout(
            gateway.merchant.id, gateway.rail_id, Decimal("50"), "USDT", "123", str(uuid4())
        )
    balance = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert balance["available"] == 0 and balance["unledgered_available"] == Decimal("500")
    key = str(uuid4())
    operation = await gateway.service.create_payout(
        gateway.merchant.id,
        gateway.rail_id,
        Decimal("50"),
        "USDT",
        "123",
        key,
        recognize_unledgered=True,
    )
    assert operation.status == "completed" and gateway.state.payout_posts == 1
    replay = await gateway.service.create_payout(
        gateway.merchant.id,
        gateway.rail_id,
        Decimal("50"),
        "USDT",
        "123",
        key,
        recognize_unledgered=True,
    )
    assert replay.id == operation.id and gateway.state.payout_posts == 1
    with pytest.raises(GatewayError, match="another payout"):
        await gateway.service.create_payout(
            gateway.merchant.id, gateway.rail_id, Decimal("50"), "USDT", "123", key
        )
    rows = await recognitions(gateway)
    assert len(rows) == 1 and int(rows[0].amount_atomic) == 5000000000
    assert rows[0].payout_operation_id == operation.id
    after = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    assert after["ledger_balance"] == 0 and after["unledgered_available"] == Decimal("450")


@pytest.mark.asyncio
async def test_recognition_excludes_ledger_obligations_and_open_invoices(gateway):
    await credit(gateway)
    gateway.state.provider_balance = Decimal("500")
    gateway.state.paid = False
    invoice = await new_invoice(gateway, amount=Decimal("100"))
    await gateway.service.select_invoice(invoice.public_id, gateway.rail_id)
    balance = (await gateway.service.balances(gateway.merchant.id, gateway.rail_id))[0]
    # 500 physical - 97 owed by ledger - 100 possibly paid but not yet ingested.
    assert balance["available"] == Decimal("97")
    assert balance["unledgered_available"] == Decimal("303")
    with pytest.raises(GatewayError, match="Insufficient"):
        await gateway.service.create_payout(
            gateway.merchant.id,
            gateway.rail_id,
            Decimal("400.000001"),
            "USDT",
            "123",
            str(uuid4()),
            recognize_unledgered=True,
        )
    assert not await recognitions(gateway)
    operation = await gateway.service.create_payout(
        gateway.merchant.id,
        gateway.rail_id,
        Decimal("400"),
        "USDT",
        "123",
        str(uuid4()),
        recognize_unledgered=True,
    )
    assert operation.status == "completed"
    rows = await recognitions(gateway)
    assert [int(row.amount_atomic) for row in rows] == [30300000000]
    assert int(rows[0].open_invoice_atomic) == 10000000000
    assert int(rows[0].ledger_outstanding_atomic) == 9700000000


@pytest.mark.asyncio
async def test_parallel_recognizing_payouts_never_recognize_the_same_funds_twice(gateway):
    gateway.state.provider_balance = Decimal("500")
    results = await asyncio.gather(
        *(
            gateway.service.create_payout(
                gateway.merchant.id,
                gateway.rail_id,
                Decimal("300"),
                "USDT",
                "123",
                str(uuid4()),
                recognize_unledgered=True,
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    operations = [r for r in results if not isinstance(r, BaseException)]
    assert len(operations) == 1
    assert any(isinstance(r, GatewayError) and r.code == "INSUFFICIENT_FUNDS" for r in results)
    assert gateway.state.payout_posts == 1 and len(await recognitions(gateway)) == 1


@pytest.mark.asyncio
async def test_database_refuses_unproven_or_mutated_recognition(gateway):
    gateway.state.provider_balance = Decimal("500")
    operation = await gateway.service.create_payout(
        gateway.merchant.id,
        gateway.rail_id,
        Decimal("10"),
        "USDT",
        "123",
        str(uuid4()),
        recognize_unledgered=True,
    )
    (row,) = await recognitions(gateway)
    async with gateway.factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE gateway_liquidity_recognitions SET amount_atomic=1 WHERE id=:id"),
                {"id": row.id},
            )
        await session.rollback()
    async with gateway.factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "INSERT INTO gateway_liquidity_recognitions(id,merchant_id,rail_id,"
                    "ledger_asset_id,amount_atomic,provider_available_atomic,"
                    "provider_reserve_atomic,ledger_outstanding_atomic,open_invoice_atomic,"
                    "payout_operation_id,evidence_digest,ledger_transaction_id,sampled_at) "
                    "SELECT :id,merchant_id,rail_id,ledger_asset_id,1,500000000,0,0,0,"
                    ":operation,evidence_digest,ledger_transaction_id,now() "
                    "FROM gateway_liquidity_recognitions WHERE id=:source"
                ),
                {"id": uuid4(), "operation": operation.id, "source": row.id},
            )
        await session.rollback()
