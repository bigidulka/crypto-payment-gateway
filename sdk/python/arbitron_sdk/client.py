"""Асинхронный HTTP-клиент шлюза."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

import aiohttp

from .models import (
    ChainInfo,
    Invoice,
    PaymentSelection,
    PaymentStatus,
    ProviderBalance,
    ProviderOperation,
)

DEFAULT_TIMEOUT_SECONDS = 30
CHAINS_CACHE_TTL_SECONDS = 300


class ArbitronError(Exception):
    """Шлюз ответил ошибкой или недоступен."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str = "",
        outcome_unknown: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.outcome_unknown = outcome_unknown


class ArbitronClient:
    """
    Клиент Merchant + Public API.

    Одна `aiohttp.ClientSession` на клиент: создавать сессию на каждый запрос
    дорого и течёт дескрипторами под нагрузкой. Используйте как async context
    manager либо вызывайте `close()` сами.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        session: aiohttp.ClientSession | None = None,
    ):
        if not api_url:
            raise ValueError("api_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session = session
        self._owns_session = session is None
        self._chains_cache: dict[str, ChainInfo] = {}
        self._chains_cached_at = 0.0

    async def __aenter__(self) -> ArbitronClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _exact_amount(value: Decimal | str) -> str:
        if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, str, int)):
            raise ValueError("Use an exact decimal amount")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError("Use an exact decimal amount") from None
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("Amount must be finite and positive")
        return str(parsed)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            async with self._get_session().request(
                method,
                f"{self._api_url}{path}",
                headers=headers,
                json=json,
                params=params,
                allow_redirects=False,
            ) as response:
                body = await response.text()
                if not 200 <= response.status < 300:
                    raise ArbitronError(
                        f"{method} {path} -> HTTP {response.status}",
                        status=response.status,
                        body=body,
                        outcome_unknown=method == "POST" and response.status >= 500,
                    )
                return await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ArbitronError(
                f"{method} request failed", outcome_unknown=method == "POST"
            ) from exc
        except TimeoutError as exc:
            raise ArbitronError(
                f"{method} request timed out", outcome_unknown=method == "POST"
            ) from exc
        except ValueError as exc:
            raise ArbitronError(
                "Gateway returned an invalid result", outcome_unknown=method == "POST"
            ) from exc

    # --- Public API (без ключа) ---

    async def get_chains(self, *, force_refresh: bool = False) -> dict[str, ChainInfo]:
        """Активные сети с токенами. Кэшируется на 5 минут."""
        now = time.monotonic()
        fresh = (now - self._chains_cached_at) < CHAINS_CACHE_TTL_SECONDS
        if self._chains_cache and fresh and not force_refresh:
            return self._chains_cache

        result = await self._request("GET", "/v1/public/chains", auth=False)
        chains: dict[str, ChainInfo] = {}
        for chain_id, data in (result.get("chains") or {}).items():
            if isinstance(data, dict):
                info = ChainInfo.from_api(chain_id, data)
                if info.is_active:
                    chains[chain_id] = info

        self._chains_cache = chains
        self._chains_cached_at = now
        return chains

    async def get_chain(self, chain: str) -> ChainInfo | None:
        return (await self.get_chains()).get(chain)

    async def get_payment_status(self, public_id: str) -> PaymentStatus:
        """Статус оплаты по публичному id. Не требует ключа."""
        data = await self._request("GET", f"/pay/{public_id}/status", auth=False)
        return PaymentStatus.from_api(data)

    async def select_payment(self, public_id: str, *, chain: str, token: str) -> PaymentSelection:
        """Выбрать сеть и токен, получить депозитный адрес под счёт."""
        data = await self._request(
            "POST",
            f"/pay/{public_id}/select",
            json={"chain": chain, "token": token.upper()},
            auth=False,
        )
        return PaymentSelection.from_api(data, chain=chain, token=token)

    # --- Merchant API (ключ обязателен) ---

    async def create_invoice(
        self,
        *,
        amount: Decimal | str,
        asset: str,
        allowed_chains: list[str],
        external_user_id: str,
        ttl_minutes: int = 60,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        allowed_rails: list[str] | None = None,
        customer_telegram_id: str = "",
    ) -> Invoice:
        """
        Выставить счёт.

        `external_user_id` - ваш идентификатор плательщика в любом виде.
        `idempotency_key` защищает от двойного счёта при ретрае: тот же ключ
        вернёт тот же счёт. Без него генерируется на каждый вызов.
        """
        if ttl_minutes < 5:
            raise ValueError("ttl_minutes must be >= 5")
        if not external_user_id:
            raise ValueError("external_user_id is required")
        if allowed_rails and not idempotency_key:
            raise ValueError("Provider invoice requires a persistent idempotency key")

        payload_meta = dict(metadata or {})
        if payload_meta.get("external_user_id", external_user_id) != external_user_id:
            raise ValueError("metadata cannot override external_user_id")
        payload_meta["external_user_id"] = external_user_id
        data = await self._request(
            "POST",
            "/v1/invoices",
            json={
                "amount": self._exact_amount(amount),
                "asset": asset.upper(),
                "allowed_chains": allowed_chains,
                "ttl_minutes": ttl_minutes,
                "metadata": payload_meta,
                "allowed_rails": allowed_rails or [],
                "customer_telegram_id": customer_telegram_id,
            },
            idempotency_key=idempotency_key or str(uuid4()),
        )
        return Invoice.from_api(data)

    async def get_invoice(self, invoice_id: str) -> Invoice:
        data = await self._request("GET", f"/v1/invoices/{invoice_id}")
        return Invoice.from_api(data)

    async def list_invoices(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Invoice]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        data = await self._request("GET", "/v1/invoices", params=params)
        items = data.get("items") if isinstance(data, dict) else data
        return [Invoice.from_api(item) for item in (items or [])]

    async def connect_provider(
        self,
        *,
        provider: str,
        token: str,
        assets: list[str],
        network: str = "MAIN_NET",
        label: str | None = None,
    ) -> str:
        data = await self._request(
            "POST",
            "/v1/rails",
            json={
                "type": provider,
                "token": token,
                "network": network,
                "assets": assets,
                "label": label,
            },
        )
        return str(data["id"])

    async def payment_methods(self) -> list[dict]:
        return (await self._request("GET", "/v1/rails"))["items"]

    async def invoice_methods(self, public_id: str) -> dict:
        return await self._request("GET", f"/pay/{public_id}/methods", auth=False)

    async def select_provider(self, public_id: str, rail_id: str) -> ProviderOperation:
        data = await self._request(
            "POST", f"/pay/{public_id}/select-provider", json={"rail_id": rail_id}, auth=False
        )
        return ProviderOperation.from_api(data)

    async def select_invoice_provider(self, invoice_id: str, rail_id: str) -> ProviderOperation:
        """Merchant-authenticated selection; replay returns the same provider invoice."""
        return ProviderOperation.from_api(
            await self._request(
                "POST", f"/v1/invoices/{invoice_id}/provider", json={"rail_id": rail_id}
            )
        )

    async def invoice_receipt(self, invoice_id: str) -> dict:
        """Verified gross/net receipt. A missing receipt never confirms payment."""
        return await self._request("GET", f"/v1/invoices/{invoice_id}/receipt")

    async def provider_balances(self, rail_id: str) -> list[ProviderBalance]:
        data = await self._request("GET", f"/v1/rails/{rail_id}/balances")
        return [ProviderBalance.from_api(row) for row in data["items"]]

    async def payout(
        self,
        *,
        rail_id: str,
        amount: Decimal | str,
        asset: str,
        destination: str,
        idempotency_key: str,
        destination_type: str = "telegram_user_id",
        recognize_unledgered_liquidity: bool = False,
    ) -> ProviderOperation:
        """Persisted payout: replay this key after a lost response; never generate a replacement.

        ``recognize_unledgered_liquidity`` lets the sole owner of a provider account
        spend funds that predate the gateway; the gateway proves and records them once.
        """
        if not idempotency_key:
            raise ValueError("Persistent payout idempotency key is required")
        body = {
            "rail_id": rail_id,
            "amount": self._exact_amount(amount),
            "asset": asset.upper(),
            "destination": destination,
            "destination_type": destination_type,
            "idempotency_key": idempotency_key,
        }
        if recognize_unledgered_liquidity:
            body["recognize_unledgered_liquidity"] = True
        data = await self._request("POST", "/v1/payouts", json=body)
        return ProviderOperation.from_api(data)

    async def get_operation(self, operation_id: str) -> ProviderOperation:
        return ProviderOperation.from_api(
            await self._request("GET", f"/v1/operations/{operation_id}")
        )

    async def reconcile_operation(self, operation_id: str) -> ProviderOperation:
        return ProviderOperation.from_api(
            await self._request("POST", f"/v1/operations/{operation_id}/reconcile")
        )

    async def onchain_balance(self, chain: str, asset: str) -> dict:
        return await self._request("GET", f"/v1/onchain/balances/{chain}/{asset}")

    async def onchain_payout(
        self,
        *,
        chain: str,
        asset: str,
        amount: Decimal | str,
        destination: str,
        idempotency_key: str,
    ) -> dict:
        if not idempotency_key:
            raise ValueError("Persistent payout key is required")
        return await self._request(
            "POST",
            "/v1/onchain/payouts",
            json={
                "chain": chain,
                "asset": asset,
                "amount": self._exact_amount(amount),
                "destination": destination,
                "idempotency_key": idempotency_key,
            },
        )

    async def onchain_operation(self, operation_id: str, *, reconcile: bool = False) -> dict:
        return await self._request(
            "POST" if reconcile else "GET",
            f"/v1/onchain/operations/{operation_id}" + ("/reconcile" if reconcile else ""),
        )
