"""Actual ASGI + PostgreSQL runtime role for tenant provisioning and chain boundaries."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from src.api.deps import get_current_merchant, get_session, require_admin_auth
from src.core.config import get_settings
from src.core.rate_limit import limiter
from src.db.models import ApiKey, Merchant, OnchainOperation
from src.main import create_app

pytest_plugins = ("tests.test_gateway_orchestration",)


async def test_managed_merchant_replay_conflict_and_secret_redaction(gateway, monkeypatch):
    app = create_app()
    monkeypatch.setattr(limiter, "enabled", False)

    async def session():
        async with gateway.factory() as current:
            yield current

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[require_admin_auth] = lambda: True
    merchant_id = str(uuid4())
    key = "isolated_generated_test_api_key_" + uuid4().hex
    payload = {"merchant_id": merchant_id, "name": "managed-test", "api_key": key}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        for _ in range(2):
            result = await client.post("/v1/admin/gateway/merchants", json=payload)
            assert result.status_code == 201 and result.json() == {"merchant_id": merchant_id}
            assert key not in result.text
        assert (
            await client.post(
                "/v1/admin/gateway/merchants",
                json={**payload, "api_key": "different_test_api_key_" + uuid4().hex},
            )
        ).status_code == 409
        short = "short-secret-sentinel"
        result = await client.post(
            "/v1/admin/gateway/merchants", json={**payload, "api_key": short}
        )
        assert result.status_code == 422 and short not in result.text and "input" not in result.text
    async with gateway.factory() as current:
        from uuid import UUID

        assert (
            await current.scalar(
                select(func.count())
                .select_from(ApiKey)
                .where(ApiKey.merchant_id == UUID(merchant_id))
            )
            == 1
        )
        assert (await current.get(Merchant, UUID(merchant_id))).name == "managed-test"


async def test_chain_api_rejects_numeric_money_and_hides_foreign_operations(gateway, monkeypatch):
    monkeypatch.setenv("ONCHAIN_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("ONCHAIN_PAYOUT_ENABLED", "true")
    get_settings.cache_clear()
    app = create_app()
    monkeypatch.setattr(limiter, "enabled", False)

    async def session():
        async with gateway.factory() as current:
            yield current

    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_merchant] = lambda: gateway.merchant
    foreign_id = uuid4()
    async with gateway.factory() as current, current.begin():
        current.add(
            OnchainOperation(
                id=foreign_id,
                merchant_id=gateway.merchants[1].id,
                purpose="funding",
                key=str(uuid4()),
                digest="a" * 64,
                chain="bsc",
                sender="0x" + "11" * 20,
                destination="0x" + "22" * 20,
                token="",
                amount_atomic=1,
                status="preparing",
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        payload = {
            "chain": "bsc",
            "asset": "USDT",
            "amount": 0.1,
            "destination": "0x" + "22" * 20,
            "idempotency_key": "stable",
        }
        assert (await client.post("/v1/onchain/payouts", json=payload)).status_code == 422
        assert (
            await client.post("/v1/onchain/payouts", json={**payload, "amount": True})
        ).status_code == 422
        assert (await client.get("/v1/onchain/balances/unknown/USDT")).status_code == 422
        assert (await client.get(f"/v1/onchain/operations/{foreign_id}")).status_code == 404
        assert (
            await client.post(f"/v1/onchain/operations/{foreign_id}/reconcile")
        ).status_code == 404


@pytest.mark.parametrize("paused", ["onchain", "ledger", "merchant"])
async def test_receipt_backed_checkout_rejects_legacy_fallback_before_address(
    gateway, monkeypatch, paused
):
    from decimal import Decimal

    from src.db.models import PaymentSession
    from src.services.invoice_service import InvoiceService

    monkeypatch.setenv("ONCHAIN_GATEWAY_ENABLED", "false" if paused == "onchain" else "true")
    monkeypatch.setenv(
        "LEDGER_RAIL_ORCHESTRATION_ENABLED", "false" if paused == "ledger" else "true"
    )
    if paused == "merchant":
        monkeypatch.setenv("LEDGER_RAIL_ORCHESTRATION_MERCHANTS", "")
    get_settings.cache_clear()
    async with gateway.factory() as current:
        stored_merchant = await current.get(Merchant, gateway.merchant.id)
        stored_merchant.name = "checkout<script>sentinel</script>"
        required = await InvoiceService(current).create_invoice(
            gateway.merchant,
            Decimal(10),
            "USDT",
            ["bsc"],
            metadata={"gateway_receipt_required": True},
            idempotency_key=str(uuid4()),
        )
        legacy = await InvoiceService(current).create_invoice(
            gateway.merchant,
            Decimal(10),
            "USDT",
            ["bsc"],
            idempotency_key=str(uuid4()),
        )
        await current.commit()
    app = create_app()

    async def session():
        async with gateway.factory() as current:
            yield current

    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(limiter, "enabled", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        response = await client.get(f"/pay/{required.public_id}/info")
        assert response.status_code == 200 and response.json()["allowed_chains"] == []
        assert (await client.get(f"/pay/{legacy.public_id}/info")).json()["allowed_chains"] == [
            "bsc"
        ]
        page = await client.get(f"/pay/{required.public_id}")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert "checkout<script>sentinel</script>" not in page.text
        assert "checkout&lt;script&gt;sentinel&lt;/script&gt;" in page.text
        response = await client.post(
            f"/pay/{required.public_id}/select", json={"chain": "bsc", "token": "USDT"}
        )
        assert response.status_code == 422
    async with gateway.factory() as current:
        assert (
            await current.scalar(
                select(func.count())
                .select_from(PaymentSession)
                .where(PaymentSession.invoice_id == required.id)
            )
            == 0
        )
