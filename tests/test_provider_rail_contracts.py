"""Provider contract checks, not evidence of provider/mainnet acceptance."""

from decimal import Decimal, localcontext

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from src.payments.rails.base import RailError, RailPayoutRequest
from src.payments.rails.cryptobot import CryptobotRail
from src.payments.rails.http import ProviderHttp
from src.payments.rails.walletpay import WalletPayRail
from src.payments.rails.xrocket import XRocketRail


@pytest.mark.asyncio
async def test_xrocket_verification_reads_received_payment_not_invoice_price():
    app = web.Application()
    calls = []

    async def invoice(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.query["invoiceId"] == "inv-1"
        calls.append("invoice")
        return web.json_response(
            {
                "id": "inv-1",
                "priceCurrency": "USDT",
                "priceAmount": "100",
                "status": "paid",
                "links": {"telegramBotLink": "https://t.me/xRocket"},
            }
        )

    async def payments(request):
        calls.append("payments")
        return web.json_response(
            {
                "items": [
                    {
                        "id": "payment-1",
                        "status": "paid",
                        "payCurrency": "USDT",
                        "receiveCurrency": "USDT",
                        "payAmount": "99",
                        "receiveAmount": "96.00000001",
                    }
                ],
                "pagination": {"next": None},
            }
        )

    app.router.add_get("/api/v1/invoice", invoice)
    app.router.add_get("/api/v1/invoice/payments", payments)
    async with TestServer(app) as server:
        rail = XRocketRail("test-token", base_url=str(server.make_url("/")))
        try:
            with localcontext() as context:
                context.prec = 8
                paid = await rail.verify_payment("inv-1")
            assert paid.amount == Decimal("99")
            assert paid.settled_amount == Decimal("96.00000001")
            assert calls == ["invoice", "payments"]
        finally:
            await rail.close()


@pytest.mark.asyncio
async def test_unstructured_mutation_failure_is_unknown_and_is_not_retried():
    app = web.Application()
    calls = []

    async def fail(request):
        calls.append(await request.json())
        return web.json_response({"message": "upstream failure"}, status=503)

    app.router.add_post("/api/v1/payouts", fail)
    async with TestServer(app) as server:
        rail = XRocketRail("test-token", base_url=str(server.make_url("/")))
        try:
            with pytest.raises(RailError) as caught:
                await rail.payout(
                    RailPayoutRequest(
                        Decimal("1"), "USDT", destination="123", operation_id="durable-op-1"
                    )
                )
            assert caught.value.outcome_unknown
            assert len(calls) == 1
            assert calls[0]["clientPayoutId"] == "durable-op-1"
        finally:
            await rail.close()


@pytest.mark.asyncio
async def test_provider_redirect_does_not_forward_credentials():
    app = web.Application()
    calls = []

    async def redirect(request):
        raise web.HTTPFound("/secret-target")

    async def target(request):
        calls.append(request.headers.get("Authorization"))
        return web.json_response({})

    app.router.add_get("/read", redirect)
    app.router.add_get("/secret-target", target)
    async with TestServer(app) as server:
        client = ProviderHttp(
            "xrocket", str(server.make_url("/")), {"Authorization": "Bearer test"}
        )
        try:
            with pytest.raises(RailError):
                await client.request("GET", "/read")
            assert calls == []
        finally:
            await client.close()


def test_wallet_pay_does_not_advertise_automatic_payout_or_spendable_balance():
    rail = WalletPayRail("test-key")
    assert rail.capabilities.payout_mode == "manual"
    assert not rail.capabilities.balances
    assert rail.capabilities.requires_telegram_customer
    assert rail.capabilities.settlement_hold_seconds == 172800


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [{"payoutId": None}, {"amount": "invalid"}, {"target": "999"}, {"targetType": "user_id"}],
)
async def test_xrocket_post_response_conflict_retains_unknown_outcome(broken):
    app = web.Application()
    calls = []

    async def payout(request):
        calls.append(await request.json())
        return web.json_response(
            {
                "payoutId": "p1",
                "clientPayoutId": "op1",
                "amount": "1",
                "asset": "USDT",
                "target": "123",
                "targetType": "telegram_user_id",
                "status": "finished",
                **broken,
            }
        )

    app.router.add_post("/api/v1/payouts", payout)
    async with TestServer(app) as server:
        rail = XRocketRail("test", base_url=str(server.make_url("/")))
        try:
            with pytest.raises(RailError) as exc:
                await rail.payout(
                    RailPayoutRequest(Decimal("1"), "USDT", "123", operation_id="op1")
                )
            assert exc.value.outcome_unknown
            assert len(calls) == 1
        finally:
            await rail.close()


@pytest.mark.asyncio
async def test_xrocket_get_payout_cannot_complete_without_money_and_target():
    app = web.Application()

    async def payout(request):
        return web.json_response({"payoutId": "p1", "clientPayoutId": "op1", "status": "finished"})

    app.router.add_get("/api/v1/payout", payout)
    async with TestServer(app) as server:
        rail = XRocketRail("test", base_url=str(server.make_url("/")))
        try:
            with pytest.raises(RailError):
                await rail.get_payout("op1")
        finally:
            await rail.close()


@pytest.mark.asyncio
async def test_cryptobot_numeric_json_fee_is_decoded_as_exact_decimal():
    app = web.Application()

    async def invoice(request):
        return web.Response(
            text='{"ok":true,"result":{"items":[{"invoice_id":1,'
            '"asset":"USDT","amount":"100","status":"paid","bot_invoice_url":"https://t.me/test",'
            '"fee_asset":"USDT","fee_amount":1.50000001}]}}',
            content_type="application/json",
        )

    app.router.add_get("/getInvoices", invoice)
    async with TestServer(app) as server:
        rail = CryptobotRail("test", base_url=str(server.make_url("/")))
        try:
            with localcontext() as context:
                context.prec = 8
                payment = await rail.verify_payment("1")
            assert payment.amount == Decimal("100")
            assert payment.settled_amount == Decimal("98.49999999")
            assert isinstance(payment.raw["fee_amount"], Decimal)
        finally:
            await rail.close()


@pytest.mark.asyncio
async def test_xrocket_terminal_zero_attempt_does_not_hide_paid_attempt():
    rail = XRocketRail("test")

    async def response(method, path, data=None, **kwargs):
        if path == "/api/v1/invoice":
            return {
                "id": "i1",
                "priceCurrency": "USDT",
                "priceAmount": "100",
                "status": "paid",
                "links": {"telegramBotLink": "https://t.me/test"},
            }
        return {
            "items": [
                {"id": "p0", "status": "expired", "payAmount": "0", "receiveAmount": "0"},
                {
                    "id": "p1",
                    "status": "paid",
                    "payCurrency": "USDT",
                    "receiveCurrency": "USDT",
                    "payAmount": "100",
                    "receiveAmount": "99",
                },
            ],
            "pagination": {"next": None},
        }

    rail._http.request = response
    paid = await rail.verify_payment("i1")
    assert (paid.amount, paid.settled_amount) == (Decimal("100"), Decimal("99"))


@pytest.mark.asyncio
async def test_wallet_reconciliation_searches_beyond_first_ten_thousand_orders():
    app = web.Application()
    offsets = []
    order = {
        "id": 10001,
        "status": "PAID",
        "amount": {"amount": "100.000001", "currencyCode": "USDT"},
        "directPayLink": "https://t.me/test",
        "externalId": "external1",
        "autoConversionCurrency": "USDT",
    }

    async def preview(request):
        return web.json_response({"status": "SUCCESS", "data": order})

    async def count(request):
        return web.json_response({"status": "SUCCESS", "data": {"totalAmount": 10001}})

    async def orders(request):
        offset = int(request.query["offset"])
        offsets.append(offset)
        items = (
            [{"id": i} for i in range(1, 10001)]
            if offset == 0
            else [
                {
                    **order,
                    "selectedPaymentOption": {
                        "amount": {"amount": "20", "currencyCode": "TON"},
                        "amountNet": {"amount": "97", "currencyCode": "USDT"},
                        "amountFee": {"amount": "3.000001", "currencyCode": "USDT"},
                        "exchangeRate": "0.2",
                    },
                }
            ]
        )
        return web.json_response({"status": "SUCCESS", "data": {"items": items}})

    app.router.add_get("/wpay/store-api/v1/order/preview", preview)
    app.router.add_get("/wpay/store-api/v1/reconciliation/order-amount", count)
    app.router.add_get("/wpay/store-api/v1/reconciliation/order-list", orders)
    async with TestServer(app) as server:
        rail = WalletPayRail("test", base_url=str(server.make_url("/")))
        try:
            with localcontext() as context:
                context.prec = 6
                paid = await rail.verify_payment("10001")
            assert (paid.amount, paid.asset, paid.settled_amount) == (
                Decimal("100.000001"),
                "USDT",
                Decimal("97"),
            )
            assert offsets == [0, 10000]
        finally:
            await rail.close()
