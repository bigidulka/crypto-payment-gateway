"""
CryptoBot-рельс: приём через createInvoice, статусы через getInvoices,
выплаты чеками через createCheck.

Портирован из payment_bot/shared/payments/cryptobot.py без бот-специфики:
никаких пинов чеков на пользователя и настроек из TOML бота - только
платёжные операции API CryptoBot (pay.crypt.bot).

Креды - токен приложения CryptoBot самого мерчанта: деньги проходят по его
аккаунту, шлюз их не касается.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from src.payments.rails.base import (
    Rail,
    RailError,
    RailInvoice,
    RailInvoiceRequest,
    RailInvoiceStatus,
    RailPayout,
    RailPayoutRequest,
    RailType,
)

API_BASE = "https://pay.crypt.bot/api"
TESTNET_API_BASE = "https://testnet-pay.crypt.bot/api"
DEFAULT_TIMEOUT_SECONDS = 30

_NETWORK_API_BASES = {
    "MAIN_NET": API_BASE,
    "TEST_NET": TESTNET_API_BASE,
}

_STATUS_MAP = {
    "active": RailInvoiceStatus.PENDING,
    "paid": RailInvoiceStatus.PAID,
    "expired": RailInvoiceStatus.EXPIRED,
}

_SUPPORTED_ASSETS = ("USDT", "USDC", "TON", "BTC", "ETH")


def _parse_retry_after(value: str | None) -> int | None:
    """Return an HTTP Retry-After delta when it is a positive integer."""
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _positive_finite_amount(value: object, field: str) -> Decimal:
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RailError("cryptobot", f"{field} is invalid", permanent=True) from exc
    if not amount.is_finite() or amount <= 0:
        raise RailError("cryptobot", f"{field} must be positive and finite", permanent=True)
    return amount



def _telegram_user_id(destination: str) -> int:
    """Validate an ASCII positive Telegram user ID for a pinned check."""
    if not destination.isascii() or not destination.isdecimal():
        raise RailError(
            "cryptobot", "positive numeric Telegram recipient is required", permanent=True
        )
    recipient_id = int(destination)
    if recipient_id <= 0:
        raise RailError(
            "cryptobot", "positive numeric Telegram recipient is required", permanent=True
        )
    return recipient_id

class CryptobotRail(Rail):
    rail_type = RailType.CRYPTOBOT

    def __init__(
        self,
        token: str,
        *,
        network: str = "MAIN_NET",
        base_url: str | None = None,
    ):
        if not token:
            raise RailError("cryptobot", "API token is required", permanent=True)
        normalized_network = network.upper()
        if normalized_network not in _NETWORK_API_BASES:
            raise RailError("cryptobot", f"unsupported network: {network}", permanent=True)
        self._token = token
        self._network = normalized_network
        # Explicit URL injection is for controlled tests or a separately
        # configured provider endpoint; it never changes the network label.
        self._base_url = (base_url or _NETWORK_API_BASES[normalized_network]).rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _api(
        self,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        *,
        mutation: bool = False,
    ) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)
            )
        headers = {"Crypto-Pay-API-Token": self._token}
        url = f"{self._base_url}/{endpoint}"
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                json=payload if method == "POST" else None,
                params=payload if method == "GET" else None,
            ) as response:
                status = response.status
                retry_after_seconds = _parse_retry_after(response.headers.get("Retry-After"))
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as exc:
                    raise RailError(
                        "cryptobot",
                        f"{endpoint} returned malformed JSON",
                        permanent=status < 500 and status not in (408, 429),
                        retry_after_seconds=retry_after_seconds,
                        outcome_unknown=mutation,
                    ) from exc
        except RailError:
            raise
        except aiohttp.ClientError as exc:
            raise RailError(
                "cryptobot", f"network error: {exc}", outcome_unknown=mutation
            ) from exc
        except TimeoutError as exc:
            raise RailError(
                "cryptobot", f"timeout on {endpoint}", outcome_unknown=mutation
            ) from exc

        if not 200 <= status < 300:
            # Never accept an `ok: true` body on a non-2xx HTTP response.
            permanent = status < 500 and status not in (408, 429)
            raise RailError(
                "cryptobot",
                f"{endpoint} failed with HTTP {status}",
                permanent=permanent,
                retry_after_seconds=retry_after_seconds,
                outcome_unknown=mutation,
            )
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise RailError(
                "cryptobot",
                f"{endpoint} returned invalid provider success envelope",
                permanent=True,
                outcome_unknown=mutation,
            )
        return body.get("result")

    # --- Rail ---

    def supported_assets(self) -> tuple[str, ...]:
        # CryptoBot itself exposes currencies; this is the explicitly supported baseline.
        return _SUPPORTED_ASSETS

    async def create_invoice(self, request: RailInvoiceRequest) -> RailInvoice:
        requested_asset = request.asset.upper()
        requested_amount = _positive_finite_amount(request.amount, "request amount")
        if requested_asset not in self.supported_assets():
            raise RailError("cryptobot", f"unsupported asset: {requested_asset}", permanent=True)
        result = await self._api(
            "POST",
            "createInvoice",
            {
                "asset": requested_asset,
                "amount": str(requested_amount),
                "description": request.description or None,
                "payload": request.payload or request.external_id or None,
                "expires_in": request.expires_in_minutes * 60,
            },
            mutation=True,
        )
        try:
            invoice = self._invoice_from_api(result)
        except RailError as exc:
            # A 2xx provider response may still represent an invoice that was
            # created but returned in an unusable form. Do not retry blindly.
            raise RailError(
                "cryptobot",
                str(exc),
                permanent=exc.permanent,
                retry_after_seconds=exc.retry_after_seconds,
                outcome_unknown=True,
            ) from exc
        if invoice.asset != requested_asset:
            raise RailError(
                "cryptobot",
                f"createInvoice asset mismatch: expected {requested_asset}, got {invoice.asset}",
                permanent=True,
                outcome_unknown=True,
            )
        if invoice.amount != requested_amount:
            raise RailError(
                "cryptobot",
                f"createInvoice amount mismatch: expected {requested_amount}, got {invoice.amount}",
                permanent=True,
                outcome_unknown=True,
            )
        return invoice

    async def get_invoice(self, provider_invoice_id: str) -> RailInvoice:
        result = await self._api("GET", "getInvoices", {"invoice_ids": provider_invoice_id})
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise RailError("cryptobot", "getInvoices returned malformed items", permanent=True)
        matches = [
            item
            for item in items
            if isinstance(item, dict) and str(item.get("invoice_id", "")) == provider_invoice_id
        ]
        if len(matches) != 1:
            raise RailError(
                "cryptobot",
                f"invoice {provider_invoice_id} missing or ambiguous in provider response",
                permanent=True,
            )
        return self._invoice_from_api(matches[0])

    async def payout(self, request: RailPayoutRequest) -> RailPayout:
        """Create a check pinned to a specific positive Telegram recipient."""
        recipient_id = _telegram_user_id(request.destination)
        requested_asset = request.asset.upper()
        if requested_asset not in self.supported_assets():
            raise RailError("cryptobot", f"unsupported asset: {requested_asset}", permanent=True)
        requested_amount = _positive_finite_amount(request.amount, "payout amount")
        result = await self._api(
            "POST",
            "createCheck",
            {
                "asset": requested_asset,
                "amount": str(requested_amount),
                "pin_to_user_id": recipient_id,
            },
            mutation=True,
        )
        if not isinstance(result, dict):
            raise RailError(
                "cryptobot",
                "createCheck returned malformed result",
                permanent=True,
                outcome_unknown=True,
            )
        provider_payout_id = str(result.get("check_id") or result.get("id") or "")
        check_url = str(result.get("bot_check_url") or "")
        if not provider_payout_id or not check_url:
            raise RailError(
                "cryptobot",
                "createCheck response lacks check id or URL",
                permanent=True,
                outcome_unknown=True,
            )
        return RailPayout(
            success=True,
            provider_payout_id=provider_payout_id,
            check_url=check_url,
            message="Check created",
            raw=result,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # --- внутреннее ---

    @staticmethod
    def _invoice_from_api(data: Any) -> RailInvoice:
        if not isinstance(data, dict):
            raise RailError("cryptobot", "provider returned malformed invoice", permanent=True)
        provider_invoice_id = str(data.get("invoice_id") or "")
        asset = str(data.get("asset") or "").upper()
        if not provider_invoice_id or asset not in _SUPPORTED_ASSETS:
            raise RailError("cryptobot", "provider invoice lacks id or valid asset", permanent=True)
        amount = _positive_finite_amount(data.get("amount"), "provider invoice amount")
        raw_status = str(data.get("status") or "").lower()
        try:
            status = _STATUS_MAP[raw_status]
        except KeyError as exc:
            raise RailError(
                "cryptobot",
                f"unknown provider invoice status: {raw_status}",
                permanent=True,
            ) from exc
        return RailInvoice(
            provider_invoice_id=provider_invoice_id,
            status=status,
            pay_url=str(data.get("bot_invoice_url", "")),
            asset=asset,
            amount=amount,
            raw=data,
        )
