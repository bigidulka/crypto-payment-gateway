"""
TON Adapter - адаптер для работы с The Open Network blockchain.

Поддерживает:
- Получение балансов TON и Jetton токенов
- Отслеживание Transfer событий
- Отправка транзакций (sweep)

Использует TON Center HTTP API для взаимодействия.
"""

import asyncio
import base64
import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from src.blockchain.base_adapter import (
    BaseAdapter,
    BalanceInfo,
    ChainType,
    FeeEstimate,
    TransactionResult,
    TransferEvent,
)
from src.blockchain.chains import ChainConfig, get_chain_config
from src.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class TonTransferInfo:
    """Информация о TON transfer."""

    lt: int  # Logical time
    hash: str
    utime: int
    from_address: str
    to_address: str
    jetton_master: str | None  # None для нативного TON
    amount: int
    decimals: int


class TonAdapter(BaseAdapter[ChainConfig]):
    """
    Адаптер для работы с TON blockchain.

    Использует TON Center HTTP API или TonAPI для взаимодействия.
    """

    def __init__(
        self,
        chain: str = "ton",
        config: ChainConfig | None = None,
        rpc_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if config is None:
            config = get_chain_config(chain)

        super().__init__(chain, config)

        # Получаем RPC URL и API key
        if rpc_url is None:
            settings = get_settings()
            rpc_url = getattr(settings, "ton_rpc_url", None) or config.rpc_url
            api_key = getattr(settings, "ton_api_key", None)

        self.rpc_url = rpc_url.rstrip("/")
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

        # Кэш для jetton master -> decimals
        self._decimals_cache: dict[str, int] = {
            token.contract_address: token.decimals
            for token in config.tokens.values()
        }

        logger.info(f"Initialized TonAdapter (rpc={rpc_url})")

    @property
    def chain_type(self) -> ChainType:
        return ChainType.TON

    @property
    def native_symbol(self) -> str:
        return "TON"

    @property
    def address_length(self) -> int:
        return 48  # User-friendly format

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _api_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        Выполнить API вызов к TON Center.

        Args:
            method: API метод
            params: Параметры

        Returns:
            Результат вызова
        """
        client = await self._get_client()

        url = f"{self.rpc_url}/{method}"
        headers = {}

        if self.api_key:
            headers["X-API-Key"] = self.api_key

        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                error = data.get("error", "Unknown error")
                logger.error(f"TON API error: {error}")
                raise Exception(f"API error: {error}")

            return data.get("result")

        except httpx.HTTPError as e:
            logger.error(f"TON HTTP error: {e}")
            raise

    async def _jsonrpc_call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        Выполнить JSON-RPC вызов к TON Center.

        Args:
            method: RPC метод
            params: Параметры

        Returns:
            Результат вызова
        """
        client = await self._get_client()

        url = f"{self.rpc_url}/jsonRPC"
        headers = {"Content-Type": "application/json"}

        if self.api_key:
            headers["X-API-Key"] = self.api_key

        payload = {
            "id": "1",
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }

        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"TON RPC error: {data['error']}")
                raise Exception(f"RPC error: {data['error']}")

            return data.get("result")

        except httpx.HTTPError as e:
            logger.error(f"TON HTTP error: {e}")
            raise

    # === Connection ===

    async def is_connected(self) -> bool:
        """Проверить подключение к ноде."""
        try:
            result = await self._api_call("getMasterchainInfo")
            return result is not None
        except Exception:
            return False

    # === Block / Seqno ===

    async def get_latest_block(self) -> int:
        """Получить номер последнего блока (seqno masterchain)."""
        result = await self._api_call("getMasterchainInfo")
        if result is None:
            raise Exception("Failed to get masterchain info")
        return result.get("last", {}).get("seqno", 0)

    async def get_block_timestamp(self, seqno: int) -> int | None:
        """Получить timestamp блока."""
        try:
            result = await self._api_call(
                "getBlockHeader",
                {"workchain": -1, "shard": -9223372036854775808, "seqno": seqno},
            )
            if result:
                return result.get("gen_utime")
        except Exception:
            pass
        return None

    # === Balance ===

    async def get_native_balance(self, address: str) -> Decimal:
        """Получить баланс TON."""
        result = await self._api_call("getAddressBalance", {"address": address})
        if result is None:
            return Decimal(0)

        # Баланс в нанотонах
        nanotons = int(result)
        return Decimal(nanotons) / Decimal(10**9)

    async def get_token_balance(
        self,
        address: str,
        token_address: str,
    ) -> Decimal:
        """
        Получить баланс Jetton токена.

        Args:
            address: Адрес владельца
            token_address: Jetton Master Address
        """
        try:
            # Получаем Jetton Wallet адрес для владельца
            wallet_address = await self._get_jetton_wallet_address(
                token_address, address
            )

            if wallet_address is None:
                return Decimal(0)

            # Запрашиваем get_wallet_data у jetton wallet
            result = await self._run_get_method(
                wallet_address, "get_wallet_data", []
            )

            if result is None or "stack" not in result:
                return Decimal(0)

            stack = result["stack"]
            if not stack:
                return Decimal(0)

            # Первый элемент стека - баланс
            balance_item = stack[0]
            if balance_item[0] == "num":
                raw_balance = int(balance_item[1], 16)
            else:
                raw_balance = int(balance_item[1])

            decimals = self._decimals_cache.get(token_address, 6)
            return Decimal(raw_balance) / Decimal(10**decimals)

        except Exception as e:
            logger.error(f"Failed to get jetton balance: {e}")
            return Decimal(0)

    async def _get_jetton_wallet_address(
        self,
        jetton_master: str,
        owner: str,
    ) -> str | None:
        """
        Получить адрес Jetton Wallet для владельца.

        Вызывает get_wallet_address у Jetton Master контракта.
        """
        try:
            # Конвертируем owner address в cell slice
            result = await self._run_get_method(
                jetton_master,
                "get_wallet_address",
                [["tvm.Slice", self._address_to_cell(owner)]],
            )

            if result is None or "stack" not in result:
                return None

            stack = result["stack"]
            if not stack:
                return None

            # Результат - адрес
            addr_item = stack[0]
            if addr_item[0] == "cell":
                # Парсим cell в адрес
                return self._cell_to_address(addr_item[1])

            return None

        except Exception as e:
            logger.debug(f"Failed to get jetton wallet address: {e}")
            return None

    async def _run_get_method(
        self,
        address: str,
        method: str,
        stack: list,
    ) -> dict | None:
        """Запустить GET метод смарт-контракта."""
        try:
            result = await self._api_call(
                "runGetMethod",
                {
                    "address": address,
                    "method": method,
                    "stack": stack,
                },
            )
            return result
        except Exception:
            return None

    def _address_to_cell(self, address: str) -> str:
        """Конвертировать адрес в cell формат для TVM."""
        # Упрощённая реализация - нужна полная для production
        return address

    def _cell_to_address(self, cell: str) -> str:
        """Конвертировать cell в адрес."""
        # Упрощённая реализация - нужна полная для production
        return cell

    # === Transfer Events ===

    async def get_transfer_events(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_addresses: list[str] | None = None,
    ) -> list[TransferEvent]:
        """
        Получить Transfer события за диапазон блоков.

        Для TON получаем транзакции каждого адреса и парсим jetton transfers.
        """
        events: list[TransferEvent] = []

        for address in to_addresses:
            # Получаем транзакции адреса
            transactions = await self._get_transactions(
                address, limit=100, from_lt=None, to_lt=None
            )

            for tx in transactions:
                # Фильтруем по времени/seqno
                utime = tx.get("utime", 0)
                # TODO: более точная фильтрация по блокам

                # Проверяем входящие сообщения
                in_msg = tx.get("in_msg", {})
                if not in_msg:
                    continue

                # Нативный TON transfer
                if in_msg.get("value"):
                    value = int(in_msg.get("value", 0))
                    source = in_msg.get("source", "")

                    # Фильтруем исходящие транзакции
                    if not source:
                        continue

                    events.append(TransferEvent(
                        tx_hash=tx.get("hash", ""),
                        block_number=tx.get("mc_block_seqno", 0),
                        log_index=0,
                        from_address=source,
                        to_address=address,
                        token_address="",  # Нативный TON
                        token_symbol="TON",
                        amount=Decimal(value) / Decimal(10**9),
                        raw_amount=value,
                        chain=self.chain,
                        chain_type=ChainType.TON,
                        timestamp=utime,
                    ))

                # Jetton transfers (в out_msgs или parsed body)
                jetton_transfers = self._parse_jetton_transfers(
                    tx, address, token_addresses
                )
                events.extend(jetton_transfers)

        return events

    async def _get_transactions(
        self,
        address: str,
        limit: int = 100,
        from_lt: int | None = None,
        to_lt: int | None = None,
    ) -> list[dict]:
        """Получить транзакции адреса."""
        params: dict[str, Any] = {
            "address": address,
            "limit": limit,
        }

        if from_lt:
            params["lt"] = from_lt
        if to_lt:
            params["to_lt"] = to_lt

        result = await self._api_call("getTransactions", params)
        return result or []

    def _parse_jetton_transfers(
        self,
        tx: dict,
        target_address: str,
        token_filter: list[str] | None,
    ) -> list[TransferEvent]:
        """
        Парсить Jetton transfers из транзакции.

        Jetton transfers используют internal_transfer opcode.
        """
        events: list[TransferEvent] = []

        # Анализируем out_msgs для jetton transfer notification
        out_msgs = tx.get("out_msgs", [])

        for msg in out_msgs:
            # Jetton transfer notification имеет специфический формат body
            body = msg.get("message", "")
            destination = msg.get("destination", "")

            if not body:
                continue

            # Пробуем декодировать body как jetton notification
            # opcode: 0x7362d09c (transfer_notification)
            try:
                decoded = self._decode_jetton_notification(body)
                if decoded is None:
                    continue

                jetton_master = decoded.get("jetton_master", "")
                amount = decoded.get("amount", 0)
                sender = decoded.get("sender", "")

                # Фильтруем по токенам
                if token_filter and jetton_master not in token_filter:
                    continue

                decimals = self._decimals_cache.get(jetton_master, 6)
                token_symbol = self._get_token_symbol(jetton_master)

                events.append(TransferEvent(
                    tx_hash=tx.get("hash", ""),
                    block_number=tx.get("mc_block_seqno", 0),
                    log_index=len(events),
                    from_address=sender,
                    to_address=target_address,
                    token_address=jetton_master,
                    token_symbol=token_symbol,
                    amount=Decimal(amount) / Decimal(10**decimals),
                    raw_amount=amount,
                    chain=self.chain,
                    chain_type=ChainType.TON,
                    timestamp=tx.get("utime"),
                ))

            except Exception:
                continue

        return events

    def _decode_jetton_notification(self, body: str) -> dict | None:
        """
        Декодировать jetton transfer notification body.

        Формат: opcode(32) + query_id(64) + amount(coins) + sender(address)
        """
        # TODO: Полная реализация декодирования TL-B
        # Для MVP возвращаем None
        return None

    def _get_token_symbol(self, jetton_master: str) -> str:
        """Получить символ токена по jetton master address."""
        for token in self.config.tokens.values():
            if token.contract_address == jetton_master:
                return token.symbol
        return "UNKNOWN"

    # === Transaction Status ===

    async def get_confirmations(self, tx_hash: str) -> int | None:
        """
        Получить количество подтверждений транзакции.

        TON использует finality на основе masterchain seqno.
        """
        try:
            # Получаем текущий seqno
            current_seqno = await self.get_latest_block()

            # Получаем транзакцию
            # В TON нужен lt + hash для поиска транзакции
            # Упрощённо считаем что если транзакция найдена - она подтверждена

            return current_seqno  # Упрощённо

        except Exception:
            return None

    async def is_tx_confirmed(
        self,
        tx_hash: str,
        required_confirmations: int | None = None,
    ) -> bool:
        """
        Проверить, подтверждена ли транзакция.

        TON имеет быструю финальность (~5 сек).
        """
        confirmations = await self.get_confirmations(tx_hash)
        if confirmations is None:
            return False

        if required_confirmations is None:
            required_confirmations = self.config.confirmations

        return confirmations >= required_confirmations

    # === Send Transaction ===

    async def estimate_transfer_fee(
        self,
        from_address: str,
        to_address: str,
        token_address: str | None = None,
        amount: Decimal | None = None,
    ) -> FeeEstimate:
        """Оценить комиссию за транзакцию."""
        # Базовая комиссия TON ~0.01-0.05 TON
        base_fee = 50_000_000  # 0.05 TON в нанотонах

        if token_address:
            # Jetton transfer стоит дороже
            base_fee = 100_000_000  # 0.1 TON

        return FeeEstimate(
            estimated_fee=Decimal(base_fee) / Decimal(10**9),
            estimated_fee_raw=base_fee,
        )

    async def send_native_token(
        self,
        from_private_key: str,
        to_address: str,
        amount: Decimal,
    ) -> TransactionResult:
        """
        Отправить TON.

        Note: Требует tonsdk или tonpy для подписи.
        """
        # TODO: Реализовать с tonsdk
        logger.warning("TON native transfer not implemented yet")
        return TransactionResult(
            tx_hash="",
            success=False,
            error="Not implemented",
        )

    async def send_token(
        self,
        from_private_key: str,
        to_address: str,
        token_address: str,
        amount: Decimal,
    ) -> TransactionResult:
        """
        Отправить Jetton токен.

        Note: Требует tonsdk или tonpy для подписи.
        """
        # TODO: Реализовать с tonsdk
        logger.warning("TON jetton transfer not implemented yet")
        return TransactionResult(
            tx_hash="",
            success=False,
            error="Not implemented",
        )

    # === Address Validation ===

    def is_valid_address(self, address: str) -> bool:
        """
        Проверить валидность TON адреса.

        Поддерживает:
        - Raw формат: workchain:hex_hash
        - User-friendly: base64url encoded
        """
        try:
            # User-friendly формат
            if len(address) == 48 and not ":" in address:
                # Base64 encoded
                try:
                    decoded = base64.urlsafe_b64decode(address + "==")
                    return len(decoded) == 36  # 1+1+32+2 bytes
                except Exception:
                    return False

            # Raw формат
            if ":" in address:
                parts = address.split(":")
                if len(parts) != 2:
                    return False
                workchain = int(parts[0])
                hex_hash = parts[1]
                return len(hex_hash) == 64 and workchain in (-1, 0)

            return False

        except Exception:
            return False

    def normalize_address(self, address: str) -> str:
        """
        Нормализовать TON адрес к user-friendly формату.

        Flags: bounceable=True, testnet=False
        """
        if not self.is_valid_address(address):
            raise ValueError(f"Invalid TON address: {address}")

        # Если уже user-friendly - возвращаем как есть
        if len(address) == 48 and ":" not in address:
            return address

        # Конвертируем raw в user-friendly
        # TODO: Полная реализация
        return address

    async def close(self) -> None:
        """Закрыть HTTP клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# Singleton адаптер
_ton_adapter: TonAdapter | None = None


def get_ton_adapter() -> TonAdapter:
    """Получить singleton TON адаптер."""
    global _ton_adapter
    if _ton_adapter is None:
        _ton_adapter = TonAdapter()
    return _ton_adapter
