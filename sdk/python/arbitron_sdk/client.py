"""Асинхронный HTTP-клиент шлюза."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any
from uuid import uuid4

import aiohttp

from .models import ChainInfo, Invoice, PaymentSelection, PaymentStatus

DEFAULT_TIMEOUT_SECONDS = 30
CHAINS_CACHE_TTL_SECONDS = 300


class ArbitronError(Exception):
    """Шлюз ответил ошибкой или недоступен."""

    def __init__(self, message: str, *, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


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

    async def __aenter__(self) -> "ArbitronClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

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
                method, f"{self._api_url}{path}", headers=headers, json=json, params=params
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise ArbitronError(
                        f"{method} {path} -> HTTP {response.status}",
                        status=response.status,
                        body=body,
                    )
                return await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise ArbitronError(f"{method} {path} failed: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise ArbitronError(f"{method} {path} timed out") from exc

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

    async def select_payment(
        self, public_id: str, *, chain: str, token: str
    ) -> PaymentSelection:
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

        payload_meta = {"external_user_id": external_user_id, **(metadata or {})}
        data = await self._request(
            "POST",
            "/v1/invoices",
            json={
                "amount": str(amount),
                "asset": asset.upper(),
                "allowed_chains": allowed_chains,
                "ttl_minutes": ttl_minutes,
                "metadata": payload_meta,
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
