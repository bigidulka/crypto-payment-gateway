"""Merchant-scoped exact on-chain liquidity and idempotent transfers."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.api.deps import MerchantDep, SessionDep
from src.blockchain.prepared import ChainEvidenceError, ChainRPCError
from src.core.rate_limit import limiter
from src.db.models import OnchainOperation
from src.services.gateway_provider_service import GatewayError
from src.services.onchain_gateway_service import OnchainGatewayService, projection

router = APIRouter(prefix="/onchain")


async def chain_call(awaitable):
    try:
        return await awaitable
    except ChainRPCError:
        raise GatewayError("Authoritative chain evidence is unavailable", status_code=503) from None
    except (ChainEvidenceError, ValueError):
        raise GatewayError(
            "Invalid chain, asset, address or exact amount", status_code=422
        ) from None


class Payout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: str = Field(min_length=1, max_length=32)
    asset: str = Field(min_length=1, max_length=10)
    amount: Decimal = Field(gt=0, max_digits=78, decimal_places=18)
    destination: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("amount", mode="before")
    @classmethod
    def exact(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Use an exact decimal string or integer")
        return value


@router.get("/balances/{chain}/{asset}")
async def balance(chain: str, asset: str, merchant: MerchantDep, session: SessionDep):
    result = await chain_call(
        OnchainGatewayService(session).balance(merchant.id, chain, asset.upper())
    )
    await session.commit()
    return result


@router.post("/payouts", status_code=202)
@limiter.limit("10/minute")
async def payout(request: Request, body: Payout, merchant: MerchantDep, session: SessionDep):
    return await chain_call(
        OnchainGatewayService(session).create_payout(
            merchant.id,
            body.chain,
            body.asset.upper(),
            body.amount,
            body.destination,
            body.idempotency_key,
        )
    )


@router.get("/operations/{operation_id}")
async def operation(operation_id: UUID, merchant: MerchantDep, session: SessionDep):
    record = await session.scalar(
        select(OnchainOperation).where(
            OnchainOperation.id == operation_id, OnchainOperation.merchant_id == merchant.id
        )
    )
    if not record:
        raise GatewayError("Operation not found", status_code=404)
    return projection(record)


@router.post("/operations/{operation_id}/reconcile")
@limiter.limit("10/minute")
async def reconcile(
    request: Request, operation_id: UUID, merchant: MerchantDep, session: SessionDep
):
    return await chain_call(OnchainGatewayService(session).reconcile(merchant.id, operation_id))
