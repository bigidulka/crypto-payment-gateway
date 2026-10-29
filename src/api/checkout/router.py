"""Merchant-authenticated Telegram checkout screens, profile and credit feed."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from src.api.deps import MerchantDep
from src.api.providers.router import ServiceDep
from src.core.rate_limit import limiter
from src.db.redis import get_redis
from src.services.telegram_checkout_service import (
    CheckoutProfileConfig,
    TelegramCheckoutService,
)

router = APIRouter(prefix="/checkout")


async def checkout_redis():
    return await get_redis()


RedisDep = Annotated[object, Depends(checkout_redis)]


def checkout_service(service: ServiceDep, redis: RedisDep) -> TelegramCheckoutService:
    return TelegramCheckoutService(service, redis)


CheckoutDep = Annotated[TelegramCheckoutService, Depends(checkout_service)]


class ScreenAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["start", "callback", "input"]
    data: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=256)


class ScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_user_id: StrictInt = Field(gt=0, lt=2**63)
    lang: str | None = Field(default=None, max_length=16)
    action: ScreenAction


@router.get("/profile")
async def get_profile(merchant: MerchantDep, checkout: CheckoutDep):
    profile = await checkout.get_profile(merchant.id)
    if profile is None:
        raise HTTPException(404, "Checkout profile is not configured")
    return profile.model_dump(mode="json")


@router.put("/profile")
@limiter.limit("10/minute")
async def put_profile(
    request: Request, body: CheckoutProfileConfig, merchant: MerchantDep, checkout: CheckoutDep
):
    return (await checkout.put_profile(merchant.id, body)).model_dump(mode="json")


@router.post("/telegram")
@limiter.limit("600/minute")
async def telegram_screen(
    request: Request, body: ScreenRequest, merchant: MerchantDep, checkout: CheckoutDep
):
    action = body.action.model_dump()
    if body.action.type == "input" and body.action.text is None:
        raise HTTPException(422, "Input action requires text")
    lang = (body.lang or "").lower()[:2] or None
    return (await checkout.handle(merchant.id, body.telegram_user_id, lang, action)).as_dict()


@router.get("/payments")
@limiter.limit("120/minute")
async def payments(
    request: Request,
    merchant: MerchantDep,
    checkout: CheckoutDep,
    limit: int = Query(50, ge=1, le=100),
    after: UUID | None = Query(None),
):
    return {"items": await checkout.payments(merchant.id, limit, after)}


@router.get("/payments/{invoice_id}")
async def payment(invoice_id: UUID, merchant: MerchantDep, checkout: CheckoutDep):
    item = await checkout.payment(merchant.id, invoice_id)
    if item is None:
        raise HTTPException(404, "Confirmed checkout payment not found")
    return item


@router.post("/payments/{invoice_id}/ack")
@limiter.limit("600/minute")
async def ack(request: Request, invoice_id: UUID, merchant: MerchantDep, checkout: CheckoutDep):
    result = await checkout.ack(merchant.id, invoice_id)
    if result is None:
        raise HTTPException(404, "Confirmed checkout payment not found")
    return result
