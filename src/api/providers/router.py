from dataclasses import asdict
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select

from src.api.deps import MerchantDep, SessionDep
from src.core.rate_limit import limiter
from src.db.models import GatewayInvoiceOptions, GatewayOperation, Invoice
from src.db.session import get_session_factory
from src.payments.rails.base import RailError
from src.services.gateway_provider_service import GatewayError, GatewayProviderService

router = APIRouter()
public_router = APIRouter(prefix="/pay")


def provider_service() -> GatewayProviderService:
    return GatewayProviderService(get_session_factory())


ServiceDep = Annotated[GatewayProviderService, Depends(provider_service)]


class ProviderConnect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["cryptobot", "xrocket", "tgwallet"]
    token: SecretStr = Field(min_length=1, max_length=2048)
    network: Literal["MAIN_NET", "TEST_NET"] = "MAIN_NET"
    assets: list[str] = Field(min_length=1, max_length=10)
    label: str | None = Field(default=None, max_length=100)

    @field_validator("assets")
    @classmethod
    def normalize_assets(cls, value):
        return [item.upper().strip() for item in value]


class ProviderSelect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rail_id: UUID


class PayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rail_id: UUID
    amount: Decimal = Field(gt=0, max_digits=78, decimal_places=18)
    asset: str = Field(min_length=1, max_length=10)
    destination: str = Field(min_length=1, max_length=32)
    destination_type: Literal["telegram_user_id", "user_id"] = "telegram_user_id"
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("amount", mode="before")
    @classmethod
    def exact_amount(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Amount must be an exact decimal string or integer")
        return value


class BalanceResponse(BaseModel):
    rail_id: UUID
    asset: str
    network: str
    ledger_balance: Decimal
    reserved: Decimal
    available: Decimal | None
    provider_available: Decimal | None
    provider_held: Decimal | None
    availability: str
    payout_mode: str
    as_of: str


class BalancesResponse(BaseModel):
    items: list[BalanceResponse]


class ReceiptEvidence(BaseModel):
    id: UUID
    event_id: str
    asset: str
    network: str
    gross: Decimal
    net: Decimal
    provider_fee: Decimal


class ReceiptResponse(BaseModel):
    invoice_id: UUID
    status: str
    receipt: ReceiptEvidence | None


async def provider_call(awaitable):
    try:
        return await awaitable
    except RailError as exc:
        raise HTTPException(503, "Provider request could not be verified") from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid provider configuration or exact amount") from exc


@router.post("/rails", status_code=201)
@limiter.limit("10/minute")
async def connect_provider(
    request: Request, body: ProviderConnect, merchant: MerchantDep, service: ServiceDep
):
    rail_id = await provider_call(
        service.connect(
            merchant.id,
            body.type,
            body.token.get_secret_value(),
            body.assets,
            network=body.network,
            label=body.label,
        )
    )
    return {"id": rail_id}


@router.get("/rails")
async def payment_methods(merchant: MerchantDep, service: ServiceDep):
    return {"items": await provider_call(service.list_methods(merchant.id))}


@router.get("/rails/{rail_id}/balances", response_model=BalancesResponse)
@limiter.limit("10/minute")
async def balances(request: Request, rail_id: UUID, merchant: MerchantDep, service: ServiceDep):
    return {"items": await provider_call(service.balances(merchant.id, rail_id))}


@router.post("/payouts", status_code=202)
@limiter.limit("10/minute")
async def payout(request: Request, body: PayoutRequest, merchant: MerchantDep, service: ServiceDep):
    return asdict(
        await provider_call(
            service.create_payout(
                merchant.id,
                body.rail_id,
                body.amount,
                body.asset,
                body.destination,
                body.idempotency_key,
                destination_type=body.destination_type,
            )
        )
    )


@router.get("/operations/{operation_id}")
async def get_operation(operation_id: UUID, merchant: MerchantDep, service: ServiceDep):
    return asdict(await service.get_operation(merchant.id, operation_id))


@router.post("/operations/{operation_id}/reconcile")
@limiter.limit("10/minute")
async def reconcile(
    request: Request, operation_id: UUID, merchant: MerchantDep, service: ServiceDep
):
    operation = await service.get_operation(merchant.id, operation_id)
    result = await provider_call(
        service.poll_invoice(merchant.id, operation_id)
        if operation.operation_type == "invoice"
        else service.reconcile_payout(merchant.id, operation_id)
    )
    return asdict(result)


@router.get("/invoices/{invoice_id}/receipt", response_model=ReceiptResponse)
async def invoice_receipt(invoice_id: UUID, merchant: MerchantDep, service: ServiceDep):
    return await service.receipt(merchant.id, invoice_id)


async def public_invoice(public_id, session, service):
    invoice = await session.scalar(select(Invoice).where(Invoice.public_id == public_id))
    if invoice is None:
        raise GatewayError("Invoice not found", status_code=404)
    service.require_enabled(invoice.merchant_id)
    return invoice


@public_router.get("/{public_id}/methods")
async def invoice_methods(public_id: str, session: SessionDep, service: ServiceDep):
    invoice = await public_invoice(public_id, session, service)
    options = await session.get(GatewayInvoiceOptions, invoice.id)
    allowed = set(options.allowed_rails) if options else set()
    # No API tokens, Telegram customer identity or account internals are public.
    methods = await service.list_methods(invoice.merchant_id)
    selected = await session.scalar(
        select(GatewayOperation).where(GatewayOperation.invoice_id == invoice.id)
    )
    return {
        "items": [
            {"id": row["id"], "type": row["type"], "label": row["label"]}
            for row in methods
            if row["id"] in allowed
        ],
        "selected": public_operation(selected) if selected else None,
    }


def public_operation(value):
    return {
        "id": str(value.id),
        "rail_id": str(value.rail_id),
        "status": value.status,
        "pay_url": value.pay_url,
    }


@public_router.post("/{public_id}/select-provider", status_code=202)
@limiter.limit("10/minute")
async def select_provider(
    request: Request, public_id: str, body: ProviderSelect, service: ServiceDep
):
    return public_operation(await provider_call(service.select_invoice(public_id, body.rail_id)))
