"""Типы ответов шлюза. Только данные, без поведения."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class InvoiceStatus(str, Enum):
    """Статусы счёта, как их отдаёт шлюз."""

    PENDING = "PENDING"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    CONFIRMING = "CONFIRMING"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    @classmethod
    def parse(cls, value: Any) -> "InvoiceStatus | str":
        text = str(value or "").upper()
        try:
            return cls(text)
        except ValueError:
            # Новый статус на стороне шлюза не должен ронять клиента
            return text


@dataclass(frozen=True)
class ChainInfo:
    """Сеть, доступная для оплаты."""

    chain_id: str
    name: str
    tokens: tuple[str, ...]
    is_active: bool = True
    confirmations: int = 0
    estimated_credit_seconds: float | None = None

    @classmethod
    def from_api(cls, chain_id: str, data: dict[str, Any]) -> "ChainInfo":
        raw_tokens = data.get("tokens") or []
        tokens = tuple(
            str(t.get("symbol") if isinstance(t, dict) else t).upper() for t in raw_tokens
        )
        est = data.get("estimated_credit_seconds")
        return cls(
            chain_id=chain_id,
            name=str(data.get("name") or chain_id.title()),
            tokens=tokens,
            is_active=bool(data.get("is_active", True)),
            confirmations=int(data.get("confirmations") or 0),
            estimated_credit_seconds=float(est) if est is not None else None,
        )


@dataclass(frozen=True)
class Invoice:
    """Счёт, созданный мерчантом."""

    invoice_id: str
    public_id: str
    hosted_url: str
    status: InvoiceStatus | str
    amount: Decimal
    asset: str
    allowed_chains: tuple[str, ...]
    expires_at: str
    payment: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Invoice":
        return cls(
            invoice_id=str(data.get("id", "")),
            public_id=str(data.get("public_id", "")),
            hosted_url=str(data.get("hosted_url", "")),
            status=InvoiceStatus.parse(data.get("status")),
            amount=Decimal(str(data.get("amount", "0"))),
            asset=str(data.get("asset", "")).upper(),
            allowed_chains=tuple(data.get("allowed_chains") or ()),
            expires_at=str(data.get("expires_at", "")),
            payment=dict(data.get("payment") or {}),
        )


@dataclass(frozen=True)
class PaymentSelection:
    """Выбранная сеть/токен и депозитный адрес под этот счёт."""

    deposit_address: str
    amount: Decimal
    chain: str
    token: str
    chain_name: str
    qr_data: str = ""
    token_contract: str = ""
    explorer_address_url: str = ""
    explorer_token_url: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any], *, chain: str, token: str) -> "PaymentSelection":
        return cls(
            deposit_address=str(data.get("deposit_address", "")),
            amount=Decimal(str(data.get("amount", "0"))),
            chain=str(data.get("chain", chain)),
            token=str(data.get("token", token)).upper(),
            chain_name=str(data.get("chain_name", chain.title())),
            qr_data=str(data.get("qr_data", "")),
            token_contract=str(data.get("token_contract", "")),
            explorer_address_url=str(data.get("explorer_address_url", "")),
            explorer_token_url=str(data.get("explorer_token_url", "")),
        )


@dataclass(frozen=True)
class PaymentStatus:
    """
    Публичный статус оплаты счёта.

    Поля про расхождение суммы заполняются, когда перевод пришёл, но счёт
    не закрыт: недоплата сверх допуска или чужой токен.
    """

    status: InvoiceStatus | str
    amount: Decimal
    asset: str
    is_expired: bool
    chain: str | None = None
    token: str | None = None
    deposit_address: str | None = None
    tx_hash: str | None = None
    confirmations: int = 0
    required_confirmations: int = 0
    received_amount: Decimal | None = None
    mismatch_reason: str | None = None
    mismatch_token: str | None = None
    missing_amount: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.status == InvoiceStatus.CONFIRMED

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "PaymentStatus":
        def dec(key: str) -> Decimal | None:
            value = data.get(key)
            return Decimal(str(value)) if value is not None else None

        return cls(
            status=InvoiceStatus.parse(data.get("status")),
            amount=Decimal(str(data.get("amount", "0"))),
            asset=str(data.get("asset", "")).upper(),
            is_expired=bool(data.get("is_expired", False)),
            chain=data.get("chain"),
            token=data.get("token"),
            deposit_address=data.get("deposit_address"),
            tx_hash=data.get("tx_hash"),
            confirmations=int(data.get("confirmations") or 0),
            required_confirmations=int(data.get("required_confirmations") or 0),
            received_amount=dec("received_amount"),
            mismatch_reason=data.get("mismatch_reason"),
            mismatch_token=data.get("mismatch_token"),
            missing_amount=dec("missing_amount"),
            raw=dict(data),
        )


@dataclass(frozen=True)
class WebhookEvent:
    """Проверенное событие вебхука."""

    event_type: str
    timestamp: int
    data: dict[str, Any]
