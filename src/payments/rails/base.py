"""
Интерфейс платёжного рельса.

Рельс - способ принять оплату. EVM-цепочка, Solana, TON, CryptoBot, xRocket,
TG Wallet - все они умеют одно и то же, насколько это нужно шлюзу:

- выставить счёт (или подготовить адрес/ссылку);
- узнать статус оплаты;
- при возможности - выплатить пользователю.

Существующий on-chain путь (invoice -> payment_session -> poller) станет
реализацией этого интерфейса для EVM; кастодиальные API реализуют его
напрямую. Инвойсный флоу шлюза выбирает рельс и дальше работает в терминах
этого интерфейса, не зная деталей провайдера.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class RailType(str, Enum):
    EVM = "evm"
    SOLANA = "solana"
    TON = "ton"
    CRYPTOBOT = "cryptobot"
    XROCKET = "xrocket"
    TGWALLET = "tgwallet"


class RailInvoiceStatus(str, Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"          # ожидает оплаты пользователем
    PAID = "PAID"                # оплачено, средства у провайдера
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class RailInvoiceRequest:
    """Что шлюз просит у рельса."""

    amount: Decimal
    asset: str
    description: str = ""
    payload: str = ""            # назначение/метка у провайдеров с мемо
    expires_in_minutes: int = 60
    # opaque-строка шлюза (обычно invoice public id): вернётся в статусе
    external_id: str = ""
    customer_telegram_id: str = ""


@dataclass(frozen=True)
class RailInvoice:
    """Счёт, выставленный рельсом."""

    provider_invoice_id: str
    status: RailInvoiceStatus
    pay_url: str = ""            # ссылка оплаты у кастодиальных
    pay_address: str = ""        # адрес у on-chain
    asset: str = ""
    amount: Decimal = Decimal("0")
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RailPayment:
    """Подтверждённая оплата."""

    provider_invoice_id: str
    amount: Decimal
    asset: str
    paid_at: str = ""            # ISO-строка от провайдера, если есть
    external_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Actual settlement in the same canonical asset, after provider fees.
    # None means that settlement evidence is unavailable, not zero fees.
    settled_amount: Decimal | None = None


@dataclass(frozen=True)
class RailPayoutRequest:
    amount: Decimal
    asset: str
    # Для CryptoBot - пользователь активирует чек сам; адрес не нужен
    destination: str = ""
    external_user_id: str = ""
    # Durable gateway operation ID, never generated afresh for a retry.
    operation_id: str = ""
    destination_type: str = "telegram_user_id"


@dataclass(frozen=True)
class RailPayout:
    success: bool
    provider_payout_id: str = ""
    check_url: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"
    amount: Decimal | None = None
    asset: str = ""
    destination: str = ""
    destination_type: str = ""


@dataclass(frozen=True)
class RailBalance:
    asset: str
    available: Decimal
    held: Decimal = Decimal("0")


@dataclass(frozen=True)
class RailCapabilities:
    payout_mode: str = "unsupported"
    balances: bool = False
    invoice_lookup_by_client_id: bool = False
    payout_lookup_by_client_id: bool = False
    requires_telegram_customer: bool = False
    settlement_hold_seconds: int = 0


class RailError(Exception):
    """A rail request failed with retry and outcome-safety metadata."""

    def __init__(
        self,
        rail: str,
        message: str,
        *,
        permanent: bool = False,
        retry_after_seconds: int | None = None,
        outcome_unknown: bool = False,
    ):
        super().__init__(f"[{rail}] {message}")
        self.rail = rail
        # permanent=True: retrying is not useful (validated credentials/4xx).
        self.permanent = permanent
        # Provider-supplied bounded delay for a retryable response, if parseable.
        self.retry_after_seconds = retry_after_seconds
        # A mutating provider call may have succeeded despite no trustworthy
        # response. Orchestration must reconcile; it must not blindly retry.
        self.outcome_unknown = outcome_unknown


class Rail(ABC):
    """
    Базовый класс рельса.

    Реализации получают уже расшифрованные креды и не хранят их дольше
    вызова. Класс лёгкий: один экземпляр на вызов, состояние не копится.
    """

    rail_type: RailType

    @property
    def capabilities(self) -> RailCapabilities:
        return RailCapabilities()

    async def account_identity(self) -> str:
        raise RailError(self.rail_type.value, "account identity lookup is unsupported", permanent=True)

    async def get_balances(self) -> tuple[RailBalance, ...]:
        raise RailError(self.rail_type.value, "authoritative balance lookup is unsupported", permanent=True)

    async def find_invoice(self, external_id: str) -> RailInvoice | None:
        raise RailError(self.rail_type.value, "client invoice lookup is unsupported", permanent=True)

    async def get_payout(self, operation_id: str, provider_payout_id: str = "") -> RailPayout:
        raise RailError(self.rail_type.value, "payout reconciliation is unsupported", permanent=True)

    @abstractmethod
    def supported_assets(self) -> tuple[str, ...]:
        """Активы, которые рельс может принять в этой конфигурации."""

    @abstractmethod
    async def create_invoice(self, request: RailInvoiceRequest) -> RailInvoice:
        """Выставить счёт у провайдера."""

    @abstractmethod
    async def get_invoice(self, provider_invoice_id: str) -> RailInvoice:
        """Текущий статус счёта у провайдера."""

    async def verify_payment(self, provider_invoice_id: str) -> RailPayment | None:
        """
        Оплата подтверждена? По умолчанию - через get_invoice.
        Рельсы с push-уведомлениями переопределяют.
        """
        invoice = await self.get_invoice(provider_invoice_id)
        if invoice.status == RailInvoiceStatus.PAID:
            return RailPayment(
                provider_invoice_id=provider_invoice_id,
                amount=invoice.amount,
                asset=invoice.asset,
                raw=invoice.raw,
            )
        return None

    async def payout(self, request: RailPayoutRequest) -> RailPayout:
        """Выплата пользователю. Рельсы без выплат кидают NotImplementedError."""
        raise NotImplementedError(f"{self.rail_type} does not support payouts")

    async def close(self) -> None:
        """Освободить HTTP-ресурсы, если есть."""
