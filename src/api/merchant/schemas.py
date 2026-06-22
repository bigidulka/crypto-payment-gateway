"""
Pydantic схемы для Merchant API.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from src.blockchain.chains import get_all_chains, get_all_tokens, get_chain_config


# === Invoice Schemas ===


class InvoiceCreateRequest(BaseModel):
    """Запрос на создание инвойса."""

    amount: Decimal = Field(
        ...,
        gt=0,
        description="Сумма платежа в USDT/USDC",
        examples=["100.50"],
    )
    asset: str = Field(
        ...,
        description="Актив для оплаты (USDT/USDC или native asset сети)",
    )
    allowed_chains: list[str] = Field(
        default_factory=lambda: get_all_chains(),
        description="Список разрешённых сетей для оплаты",
        examples=[["base", "arbitrum", "bsc"]],
    )
    ttl_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,  # Максимум 24 часа
        description="Время жизни инвойса в минутах",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Произвольные метаданные (order_id, comment и т.д.)",
        examples=[{"order_id": "ORD-12345", "comment": "Payment for order"}],
    )

    @field_validator("asset")
    @classmethod
    def normalize_asset(cls, v: str) -> str:
        asset = v.upper().strip()
        if not asset:
            raise ValueError("Asset is required")
        if asset not in get_all_tokens():
            raise ValueError(f"Invalid asset: {asset}. Supported: {get_all_tokens()}")
        return asset

    @field_validator("allowed_chains")
    @classmethod
    def validate_chains(cls, v: list[str]) -> list[str]:
        valid_chains = get_all_chains()
        for chain in v:
            if chain.lower() not in valid_chains:
                raise ValueError(f"Invalid chain: {chain}. Supported: {valid_chains}")
        return [c.lower() for c in v]

    @model_validator(mode="after")
    def validate_asset_supported_on_chains(self) -> "InvoiceCreateRequest":
        invalid = [
            chain
            for chain in self.allowed_chains
            if not get_chain_config(chain).supports_asset(self.asset)
        ]
        if invalid:
            raise ValueError(f"Asset {self.asset} is not supported on chains: {invalid}")
        return self


class PaymentInfo(BaseModel):
    """Информация о платеже (если есть)."""

    chain: str
    token: str
    deposit_address: str
    tx_hash: str | None = None
    confirmations: int = 0
    required_confirmations: int
    confirmed_at: datetime | None = None


class InvoiceResponse(BaseModel):
    """Ответ с данными инвойса."""

    id: UUID
    public_id: str
    amount: Decimal
    asset: str
    allowed_chains: list[str]
    status: str
    ttl_minutes: int
    expires_at: datetime
    metadata: dict[str, Any] | None = None
    hosted_url: str
    payment: PaymentInfo | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InvoiceListResponse(BaseModel):
    """Ответ со списком инвойсов."""

    items: list[InvoiceResponse]
    total: int
    limit: int
    offset: int


# === Webhook Schemas ===


class WebhookCreateRequest(BaseModel):
    """Запрос на создание webhook."""

    url: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="URL для отправки webhook",
        examples=["https://example.com/webhook"],
    )
    events: list[str] = Field(
        default=[
            "invoice.created",
            "invoice.seen_onchain",
            "invoice.confirmed",
            "invoice.expired",
        ],
        description="Список событий для отправки",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        valid_events = {
            "invoice.created",
            "invoice.seen_onchain",
            "invoice.confirmed",
            "invoice.expired",
        }
        for event in v:
            if event not in valid_events:
                raise ValueError(f"Invalid event: {event}. Supported: {valid_events}")
        return v


class WebhookResponse(BaseModel):
    """Ответ с данными webhook."""

    id: UUID
    url: str
    secret: str  # Показывается только при создании
    events: list[str]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class WebhookListResponse(BaseModel):
    """Ответ со списком webhooks."""

    items: list[WebhookResponse]


# === Error Schemas ===


class ErrorResponse(BaseModel):
    """Ответ с ошибкой."""

    code: str
    message: str
    details: dict[str, Any] | None = None


# === Sweep Job Schemas ===


class SweepJobResponse(BaseModel):
    """Ответ с данными sweep job."""

    id: UUID
    invoice_id: UUID
    invoice_public_id: str
    chain: str
    token: str
    deposit_address: str
    amount: Decimal
    state: str
    gas_tx_hash: str | None = None
    sweep_tx_hash: str | None = None
    attempts: int
    last_error: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class SweepJobListResponse(BaseModel):
    """Ответ со списком sweep jobs."""

    items: list[SweepJobResponse]
    total: int


# === Wallet Balance Schemas ===


class TokenBalance(BaseModel):
    """Баланс токена."""

    token: str
    balance: Decimal
    contract: str


class WalletBalance(BaseModel):
    """Баланс кошелька."""

    address: str
    chain: str
    native_balance: Decimal
    native_symbol: str
    tokens: list[TokenBalance]
    invoice_public_id: str | None = None
    invoice_status: str | None = None


class WalletBalancesResponse(BaseModel):
    """Ответ со списком балансов."""

    items: list[WalletBalance]
    total: int
    total_usdt: Decimal
    total_usdc: Decimal


# === Manual Sweep Schemas ===


class ManualSweepRequest(BaseModel):
    """Запрос на ручной sweep."""

    address: str = Field(..., description="Deposit address для sweep")
    chain: str = Field(..., description="Сеть (base, arbitrum, bsc)")
    token: str = Field(default="USDT", description="Токен (USDT, USDC)")


class ManualSweepResponse(BaseModel):
    """Ответ на ручной sweep."""

    sweep_job_id: UUID | None = None
    status: str
    message: str
    balance: Decimal | None = None
