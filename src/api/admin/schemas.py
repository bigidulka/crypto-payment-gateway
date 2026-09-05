"""
Pydantic схемы для Admin API.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# === Auth ===


class LoginRequest(BaseModel):
    """Запрос на вход в админку."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Ответ на вход."""

    success: bool
    token: str | None = None
    message: str | None = None


# === Merchants ===


class MerchantListItem(BaseModel):
    """Мерчант в списке."""

    id: str
    name: str
    email: str
    is_active: bool
    created_at: datetime
    api_key_preview: str | None = None  # Первые 8 символов
    invoices_count: int = 0
    total_volume: Decimal = Decimal("0")


class MerchantListResponse(BaseModel):
    """Список мерчантов."""

    items: list[MerchantListItem]
    total: int


class MerchantCreateRequest(BaseModel):
    """Admin-only merchant onboarding request without raw secret fields."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., min_length=3, max_length=255)
    key_name: str = Field(default="default", min_length=1, max_length=100)

    @field_validator("name", "key_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value is required")
        return value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", value):
            raise ValueError("valid email is required")
        return value


class MerchantApiKeyCreateRequest(BaseModel):
    """Name for an additional merchant API key."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="extra", min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name is required")
        return value


# === System Status ===


class ChainStatus(BaseModel):
    """Статус сети."""

    chain: str
    chain_name: str
    native_symbol: str
    last_scanned_block: int | None = None
    latest_block: int | None = None
    blocks_behind: int | None = None
    is_healthy: bool = True
    last_error: str | None = None
    gas_price_gwei: float | None = None


class WorkerStatus(BaseModel):
    """Статус воркера."""

    name: str
    is_running: bool
    last_activity: datetime | None = None
    processed_count: int = 0
    error_count: int = 0
    last_error: str | None = None


class FunderStatus(BaseModel):
    """Статус funder кошелька."""

    address: str
    balances: dict[str, float] = Field(default_factory=dict)  # chain -> balance
    low_balance_chains: list[str] = Field(default_factory=list)


class SystemStatusResponse(BaseModel):
    """Общий статус системы."""

    status: Literal["healthy", "degraded", "critical"]
    timestamp: datetime
    database_connected: bool = True
    redis_connected: bool = True
    chains: list[ChainStatus] = Field(default_factory=list)
    workers: list[WorkerStatus] = Field(default_factory=list)
    funder: FunderStatus | None = None

    # Общая статистика
    total_invoices: int = 0
    pending_invoices: int = 0
    completed_invoices_24h: int = 0
    failed_sweeps: int = 0
    pending_sweeps: int = 0


# === Invoices ===


class InvoiceListItem(BaseModel):
    """Инвойс в списке."""

    id: str
    public_id: str
    status: str
    amount: Decimal
    asset: str
    created_at: datetime
    expires_at: datetime
    is_expired: bool

    # Merchant
    merchant_id: str | None = None
    merchant_name: str | None = None

    # Payment info
    chain: str | None = None
    token: str | None = None
    deposit_address: str | None = None

    # Transaction
    tx_hash: str | None = None
    confirmations: int = 0
    required_confirmations: int = 0

    # Sweep status
    sweep_state: str | None = None


class TokenBalance(BaseModel):
    """Баланс токена."""

    token: str
    balance: str  # В формате строки для фронтенда


class WalletBalanceItem(BaseModel):
    """Адрес с балансом (сгруппированный по адресу)."""

    type: Literal["wallet_address", "deposit_address", "persistent_address"]
    chain: str
    address: str
    tokens: list[TokenBalance] = Field(default_factory=list)
    native_balance: str  # В формате ETH (не wei) как строка
    native_symbol: str

    # Для wallet_address
    user_id: str | None = None

    # Для deposit_address
    invoice_id: str | None = None
    merchant_name: str | None = None


class CheckAllBalancesResponse(BaseModel):
    """Результат проверки всех балансов."""

    total_addresses_checked: int
    addresses_with_balance: int
    total_balances: dict[str, str] = Field(
        default_factory=dict
    )  # "CHAIN/TOKEN" -> balance (строка)
    items: list[WalletBalanceItem] = Field(default_factory=list)


class InvoiceListResponse(BaseModel):
    """Ответ со списком инвойсов."""

    items: list[InvoiceListItem]
    total: int
    page: int
    per_page: int
    pages: int


class InvoiceFilters(BaseModel):
    """Фильтры для списка инвойсов."""

    status: str | None = None
    chain: str | None = None
    merchant_id: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


# === Sweeps ===


class SweepListItem(BaseModel):
    """Sweep job в списке."""

    id: str
    state: str
    chain: str
    token: str
    amount: Decimal

    deposit_address: str
    treasury_address: str

    gas_tx_hash: str | None = None
    sweep_tx_hash: str | None = None

    attempts: int
    max_attempts: int
    last_error: str | None = None
    next_retry_at: datetime | None = None

    created_at: datetime
    updated_at: datetime

    # Related invoice
    invoice_id: str | None = None
    invoice_public_id: str | None = None


class SweepListResponse(BaseModel):
    """Ответ со списком sweep jobs."""

    items: list[SweepListItem]
    total: int
    page: int
    per_page: int
    pages: int


# === Logs ===


class SystemLogEntry(BaseModel):
    """Запись лога."""

    id: str
    timestamp: datetime
    level: Literal["info", "warning", "error", "critical"]
    source: str  # poller, sweeper, webhook, rpc, etc.
    chain: str | None = None
    message: str
    details: dict | None = None

    # Related entities
    invoice_id: str | None = None
    sweep_id: str | None = None
    tx_hash: str | None = None


class SystemLogsResponse(BaseModel):
    """Ответ с логами."""

    items: list[SystemLogEntry]
    total: int
    page: int
    per_page: int


class LogFilters(BaseModel):
    """Фильтры для логов."""

    level: str | None = None
    source: str | None = None
    chain: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


# === Dashboard Stats ===


class DashboardStats(BaseModel):
    """Статистика для дашборда."""

    # Invoices
    total_invoices: int = 0
    invoices_by_status: dict[str, int] = Field(default_factory=dict)

    # Volume
    volume_24h: Decimal = Decimal("0")
    volume_7d: Decimal = Decimal("0")
    volume_30d: Decimal = Decimal("0")

    # By chain
    volume_by_chain: dict[str, Decimal] = Field(default_factory=dict)
    invoices_by_chain: dict[str, int] = Field(default_factory=dict)

    # Sweeps
    total_sweeps: int = 0
    completed_sweeps: int = 0
    failed_sweeps: int = 0
    pending_sweeps: int = 0

    # Errors (last 24h)
    rpc_errors: int = 0
    sweep_errors: int = 0
    webhook_errors: int = 0


# === Actions ===


class RetrySweepRequest(BaseModel):
    """Запрос на повтор sweep."""

    sweep_id: str


class ResetSweepRequest(BaseModel):
    """Запрос на сброс sweep."""

    sweep_id: str
    reset_to_state: Literal["pending_gas", "funding"] = "pending_gas"


class ActionResponse(BaseModel):
    """Ответ на действие."""

    success: bool
    message: str


# === Withdraw ===


class WithdrawRequest(BaseModel):
    """Запрос на вывод средств с treasury."""

    chain: str = Field(..., description="Сеть (base, arbitrum, bsc, polygon, avax, optimism, solana, ton)")
    token: str = Field(..., description="Токен (USDT, USDC) или 'native' для нативной валюты")
    to_address: str = Field(..., description="Адрес получателя")
    amount: str = Field(..., description="Сумма для вывода (human-readable, например '100.50')")


class WithdrawResponse(BaseModel):
    """Ответ на вывод средств."""

    success: bool
    tx_hash: str | None = None
    chain: str
    token: str
    amount: str
    to_address: str
    message: str | None = None
    explorer_url: str | None = None
