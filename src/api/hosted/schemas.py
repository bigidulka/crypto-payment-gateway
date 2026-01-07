"""
Pydantic схемы для Hosted Payment Pages.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class PaymentSelectRequest(BaseModel):
    """Запрос на выбор сети и токена для оплаты."""

    chain: str = Field(
        ..., description="Выбранная сеть", examples=["base", "arbitrum", "bsc"]
    )
    token: str = Field(..., description="Выбранный токен", examples=["USDT", "USDC"])


class PaymentSelectResponse(BaseModel):
    """Ответ с данными для оплаты."""

    deposit_address: str = Field(..., description="Адрес для отправки токенов")
    amount: Decimal = Field(..., description="Сумма к оплате")
    chain: str
    token: str
    chain_name: str = Field(..., description="Человеко-читаемое имя сети")
    qr_data: str = Field(..., description="Данные для QR кода")
    explorer_address_url: str = Field(..., description="URL адреса в эксплорере")
    token_contract: str = Field(..., description="Адрес контракта токена")
    explorer_token_url: str = Field(
        ..., description="URL контракта токена в эксплорере"
    )


class PaymentStatusResponse(BaseModel):
    """Ответ со статусом оплаты."""

    invoice_id: str
    public_id: str
    status: str
    amount: Decimal
    asset: str
    expires_at: datetime
    is_expired: bool

    # Данные о платеже (если выбрана сеть)
    chain: str | None = None
    chain_name: str | None = None
    token: str | None = None
    deposit_address: str | None = None
    token_contract: str | None = None
    explorer_address_url: str | None = None
    explorer_token_url: str | None = None

    # Данные о транзакции (если найдена)
    tx_hash: str | None = None
    confirmations: int = 0
    required_confirmations: int = 0
    explorer_tx_url: str | None = None


class InvoiceInfoResponse(BaseModel):
    """Информация об инвойсе для hosted страницы."""

    public_id: str
    amount: Decimal
    asset: str
    status: str
    allowed_chains: list[str]
    expires_at: datetime
    is_expired: bool
    merchant_name: str
    ttl_minutes: int

    # Информация о выбранной сети (если есть)
    selected_chain: str | None = None
    selected_chain_name: str | None = None
    selected_token: str | None = None
    deposit_address: str | None = None
    token_contract: str | None = None
    explorer_address_url: str | None = None
    explorer_token_url: str | None = None
