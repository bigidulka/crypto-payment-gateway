"""Real ASGI boundary checks; no database, provider or external network I/O."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from src.api.deps import get_current_merchant, get_session
from src.api.providers.router import provider_service
from src.core.exceptions import ValidationError
from src.core.rate_limit import limiter
from src.main import create_app
from src.services.gateway_provider_service import GatewayError
from src.services.invoice_service import InvoiceService


@pytest_asyncio.fixture
async def api(monkeypatch):
    service = SimpleNamespace(get_operation=AsyncMock(), create_payout=AsyncMock())
    merchant = SimpleNamespace(id=uuid4())
    app = create_app()
    monkeypatch.setattr(limiter, "enabled", False)

    async def current_merchant():
        return merchant

    async def current_session():
        yield SimpleNamespace()

    app.dependency_overrides[get_current_merchant] = current_merchant
    app.dependency_overrides[get_session] = current_session
    app.dependency_overrides[provider_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://gateway.test",
    ) as client:
        yield client, service
    app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(404, "NOT_FOUND"), (503, "GATEWAY_DISABLED")])
async def test_domain_failures_preserve_http_status_without_internal_details(api, status, code):
    client, service = api
    service.get_operation.side_effect = GatewayError(
        "Operation unavailable", code=code, status_code=status
    )
    response = await client.get(f"/v1/operations/{uuid4()}")
    assert response.status_code == status
    assert response.json() == {"code": code, "detail": "Operation unavailable"}


@pytest.mark.asyncio
async def test_insufficient_funds_is_a_known_rejection(api):
    client, service = api
    service.create_payout.side_effect = GatewayError(
        "Insufficient verified available balance", code="INSUFFICIENT_FUNDS", status_code=422
    )
    response = await client.post(
        "/v1/payouts",
        json={
            "rail_id": str(uuid4()),
            "amount": "1",
            "asset": "USDT",
            "destination": "123",
            "idempotency_key": "durable-order",
        },
    )
    assert response.status_code == 422 and response.json()["code"] == "INSUFFICIENT_FUNDS"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0.123456789012345678", "true"])
async def test_numeric_or_boolean_money_never_reaches_payout_service(api, value):
    client, service = api
    payload = (
        '{"rail_id":"'
        + str(uuid4())
        + '","amount":'
        + value
        + ',"asset":"ETH","destination":"123","idempotency_key":"order"}'
    )
    response = await client.post(
        "/v1/payouts", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    service.create_payout.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0.12345678901234568, True, "1e19", "0.0000000000000000001"])
async def test_invalid_invoice_money_never_reaches_business_service(api, monkeypatch, amount):
    client, _ = api
    create = AsyncMock()
    monkeypatch.setattr(InvoiceService, "create_invoice", create)
    response = await client.post(
        "/v1/invoices", json={"amount": amount, "asset": "USDT", "allowed_rails": [str(uuid4())]}
    )
    assert response.status_code == 422
    create.assert_not_called()


@pytest.mark.asyncio
async def test_decimal_string_reaches_business_service_unchanged(api, monkeypatch):
    client, _ = api
    create = AsyncMock(side_effect=ValidationError("Stopped before all financial I/O"))
    monkeypatch.setattr(InvoiceService, "create_invoice", create)
    response = await client.post(
        "/v1/invoices",
        json={"amount": "0.123456789012345678", "asset": "ETH", "allowed_rails": [str(uuid4())]},
    )
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_ERROR"
    assert create.call_args.kwargs["amount"] == Decimal("0.123456789012345678")
