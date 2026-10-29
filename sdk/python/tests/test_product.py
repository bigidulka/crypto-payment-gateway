"""Real SDK HTTP and durable SQLite: no provider SDK or Telegram dependency."""

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arbitron_sdk import ArbitronClient, ArbitronError, ProductPayments
from arbitron_sdk.webhooks import compute_signature


@pytest_asyncio.fixture
async def shop(tmp_path):
    state = SimpleNamespace(invoices={}, keys=[], lose=False, paid=False,
                            wrong_customer=False, receipt_overrides={})

    async def create(request):
        assert request.headers["Authorization"] == "Bearer product-merchant"
        key = request.headers["Idempotency-Key"]
        state.keys.append(key)
        body = await request.json()
        if key not in state.invoices:
            state.invoices[key] = {
                "id": str(uuid4()), "public_id": "PAY_" + key,
                "hosted_url": "https://checkout.invalid/pay/" + key,
                "status": "AWAITING_PAYMENT", **body,
            }
        if state.lose:
            state.lose = False
            return web.json_response({}, status=503)
        return web.json_response(state.invoices[key])

    async def invoice(request):
        row = next(v for v in state.invoices.values() if v["id"] == request.match_info["id"])
        row = {**row, "metadata": dict(row["metadata"])}
        if state.wrong_customer:
            row["metadata"]["external_user_id"] = "another-product:customer"
        if state.paid:
            row["status"] = "CONFIRMED"
        return web.json_response(row)

    async def receipt(request):
        return web.json_response({
            "invoice_id": request.match_info["id"], "status": "CONFIRMED",
            "receipt": {"event_id": "event-" + request.match_info["id"],
                        "asset": "USDT", "network": "cryptobot:MAIN_NET",
                        "gross": "100", "net": "97", "provider_fee": "3",
                        **state.receipt_overrides},
        })

    app = web.Application()
    app.router.add_post("/v1/invoices", create)
    app.router.add_get("/v1/invoices/{id}", invoice)
    app.router.add_get("/v1/invoices/{id}/receipt", receipt)
    async with TestServer(app) as server:
        async with ArbitronClient(str(server.make_url("/")), "product-merchant") as client:
            yield SimpleNamespace(state=state, client=client, path=tmp_path / "product.db")


async def order(shop, product=None, **kwargs):
    product = product or ProductPayments(shop.client, shop.path)
    key = uuid4()
    result = await product.create_order(
        key, "bot:customer", "100", "USDT", **({"rail_id": "provider-id"} | kwargs)
    )
    return product, key, result


@pytest.mark.asyncio
async def test_lost_response_survives_restart_and_same_order_key(shop):
    product = ProductPayments(shop.client, shop.path)
    key = uuid4()
    shop.state.lose = True
    with pytest.raises(ArbitronError) as error:
        await product.create_order(key, "bot:customer", "100", "USDT", "provider-id")
    assert error.value.outcome_unknown
    restarted = ProductPayments(shop.client, shop.path)
    result = await restarted.create_order(key, "bot:customer", "100.00", "USDT", "provider-id")
    assert len(set(shop.state.keys)) == 1 and len(shop.state.invoices) == 1
    assert result["hosted_url"].startswith("https://checkout.invalid/")
    assert not restarted.pending_fulfillments()
    with pytest.raises(ValueError, match="another purchase"):
        await restarted.create_order(key, "bot:other", "100", "USDT", "provider-id")


@pytest.mark.asyncio
async def test_polling_fulfillment_survives_delivery_failure_and_replay(shop):
    product, key, _ = await order(shop)
    assert not await product.reconcile_order(key)
    shop.state.paid = True
    other = ProductPayments(shop.client, shop.path)
    assert sorted(await asyncio.gather(
        product.reconcile_order(key), other.reconcile_order(key)
    )) == [False, True]
    # A crash after confirmation but before granting access retains delivery.
    restarted = ProductPayments(shop.client, shop.path)
    pending = restarted.pending_fulfillments()
    assert len(pending) == 1 and pending[0]["customer"] == "bot:customer"
    assert pending[0]["receipt"]["net"] == "97"
    with pytest.raises(ValueError, match="Unknown fulfillment"):
        restarted.acknowledge_fulfillment(key, "another-event")
    assert len(restarted.pending_fulfillments()) == 1
    restarted.acknowledge_fulfillment(key, pending[0]["event_id"])
    assert not restarted.pending_fulfillments()
    assert not await restarted.reconcile_order(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"net": "100"}, {"net": "NaN"}, {"gross": "99", "net": "96"},
    {"net": "97.0000000000000000000000000001"},
    {"asset": "USDC"}, {"provider_fee": "-1", "net": "101"}, {"event_id": ""},
])
async def test_unusable_receipt_never_grants_access(shop, overrides):
    product, key, _ = await order(shop)
    shop.state.paid = True
    shop.state.receipt_overrides = overrides
    with pytest.raises(ValueError):
        await product.reconcile_order(key)
    assert not product.pending_fulfillments()


@pytest.mark.asyncio
async def test_gateway_customer_binding_and_reserved_metadata(shop):
    product, key, _ = await order(shop)
    shop.state.paid = True
    shop.state.wrong_customer = True
    with pytest.raises(ValueError, match="does not match"):
        await product.reconcile_order(key)
    assert not product.pending_fulfillments()
    posts = len(shop.state.keys)
    with pytest.raises(ValueError, match="cannot override"):
        await shop.client.create_invoice(
            amount="100", asset="USDT", allowed_chains=["bsc"],
            external_user_id="owner", metadata={"external_user_id": "another"},
        )
    assert len(shop.state.keys) == posts


@pytest.mark.asyncio
async def test_signed_webhook_and_poll_share_one_pending_delivery(shop):
    product = ProductPayments(shop.client, shop.path, "product-secret")
    _, key, created = await order(shop, product)
    shop.state.paid = True
    body = json.dumps({"event": "invoice.confirmed",
                       "event_id": "event-" + created["invoice_id"],
                       "invoice": {"id": created["invoice_id"]}}).encode()
    timestamp = int(time.time())
    headers = {"X-Webhook-Timestamp": str(timestamp),
               "X-Webhook-Event": "invoice.confirmed",
               "X-Webhook-Signature": compute_signature(body, "product-secret", timestamp)}
    assert await product.receive_webhook(body, headers)
    assert not await product.reconcile_order(key)
    assert not await product.receive_webhook(body, headers)
    assert len(product.pending_fulfillments()) == 1
    with pytest.raises(ValueError):
        await product.receive_webhook(body + b" ", headers)


@pytest.mark.asyncio
async def test_hosted_checkout_can_offer_chains_without_provider_knowledge(shop):
    product = ProductPayments(shop.client, shop.path)
    key = uuid4()
    await product.create_order(key, "any-framework:42", "100", "USDT",
                               allowed_chains=["solana", "ton", "bsc"])
    payload = shop.state.invoices[str(key)]
    assert payload["allowed_rails"] == []
    assert set(payload["allowed_chains"]) == {"solana", "ton", "bsc"}
    assert payload["metadata"]["gateway_receipt_required"] is True
