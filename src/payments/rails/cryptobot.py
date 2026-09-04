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

import asyncio
from decimal import Decimal
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
DEFAULT_TIMEOUT_SECONDS = 30

_STATUS_MAP = {
    "active": RailInvoiceStatus.PENDING,
    "paid": RailInvoiceStatus.PAID,
    "expired": RailInvoiceStatus.EXPIRED,
}


class CryptobotRail(Rail):
    rail_type = RailType.CRYPTOBOT

    def __init__(self, token: str, *, network: str = "MAIN_NET", base_url: str = API_BASE):
        if not token:
            raise RailError("cryptobot", "API token is required", permanent=True)
        self._token = token
        self._network = network
        self._base_url = base_url
        self._session: aiohttp.ClientSession | None = None

    async def _api(self, method: str, endpoint: str, payload: dict | None = None) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)
            )
        headers = {"Crypto-Pay-API-Token": self._token}
        url = f"{self._base_url}/{endpoint}"
        try:
            async with self._session.request(
                method, url, headers=headers, json=payload if method == "POST" else None,
                params=payload if method == "GET" else None,
            ) as response:
                body = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise RailError("cryptobot", f"network error: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise RailError("cryptobot", f"timeout on {endpoint}") from exc

        if not isinstance(body, dict) or not body.get("ok"):
            raise RailError(
                "cryptobot",
                f"{endpoint} failed: {body.get('error', body) if isinstance(body, dict) else body}",
                permanent=response.status < 500,
            )
        return body.get("result")

    # --- Rail ---

    def supported_assets(self) -> tuple[str, ...]:
        # CryptoBot сам сообщает список через getCurrencies; базовый минимум
        return ("USDT", "USDC", "TON", "BTC", "ETH")

    async def create_invoice(self, request: RailInvoiceRequest) -> RailInvoice:
        result = await self._api(
            "POST",
            "createInvoice",
            {
                "asset": request.asset.upper(),
                "amount": str(request.amount),
                "description": request.description or None,
                "payload": request.payload or request.external_id or None,
                "expires_in": request.expires_in_minutes * 60,
            },
        )
        return self._invoice_from_api(result)

    async def get_invoice(self, provider_invoice_id: str) -> RailInvoice:
        result = await self._api("GET", "getInvoices", {"invoice_ids": provider_invoice_id})
        items = result.get("items") if isinstance(result, dict) else result
        if not items:
            raise RailError("cryptobot", f"invoice {provider_invoice_id} not found")
        return self._invoice_from_api(items[0])

    async def payout(self, request: RailPayoutRequest) -> RailPayout:
        """Чек CryptoBot: пользователь забирает его сам в боте."""
        result = await self._api(
            "POST",
            "createCheck",
            {
                "asset": request.asset.upper(),
                "amount": str(request.amount),
                "pin_to_user_id": int(request.destination)
                if request.destination.isdigit()
                else None,
            },
        )
        return RailPayout(
            success=True,
            provider_payout_id=str(result.get("check_id") or result.get("id") or ""),
            check_url=str(result.get("bot_check_url", "")),
            message="Check created",
            raw=result,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # --- внутреннее ---

    @staticmethod
    def _invoice_from_api(data: dict[str, Any]) -> RailInvoice:
        status = _STATUS_MAP.get(
            str(data.get("status", "")).lower(), RailInvoiceStatus.PENDING
        )
        return RailInvoice(
            provider_invoice_id=str(data.get("invoice_id", "")),
            status=status,
            pay_url=str(data.get("bot_invoice_url", "")),
            asset=str(data.get("asset", "")).upper(),
            amount=Decimal(str(data.get("amount", "0"))),
            raw=data,
        )
