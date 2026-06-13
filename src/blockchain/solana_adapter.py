"""
Solana Adapter - адаптер для работы с Solana blockchain.

Поддерживает:
- Получение балансов SOL и SPL токенов
- Отслеживание Transfer событий
- Отправка транзакций (sweep)

Использует solana-py для взаимодействия с RPC.
"""

import asyncio
import base58
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

# SPL Token Program ID
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Token-2022 Program ID (для USDC и новых токенов)
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class SolanaTransferInfo:
    """Информация о Solana transfer."""

    signature: str
    slot: int
    block_time: int | None
    from_address: str
    to_address: str
    mint: str  # Token mint address
    amount: int  # Raw amount
    decimals: int


class SolanaAdapter(BaseAdapter[ChainConfig]):
    """
    Адаптер для работы с Solana blockchain.

    Использует JSON-RPC API для взаимодействия с нодой.
    """

    def __init__(
        self,
        chain: str = "solana",
        config: ChainConfig | None = None,
        rpc_url: str | None = None,
    ) -> None:
        if config is None:
            config = get_chain_config(chain)

        super().__init__(chain, config)

        # Получаем RPC URL
        if rpc_url is None:
            settings = get_settings()
            rpc_url = getattr(settings, "solana_rpc_url", None) or config.rpc_url

        self.rpc_url = rpc_url
        self._client: httpx.AsyncClient | None = None

        # Кэш для mint -> decimals
        self._decimals_cache: dict[str, int] = {
            token.contract_address: token.decimals
            for token in config.tokens.values()
        }

        logger.info(f"Initialized SolanaAdapter (rpc={rpc_url})")

    @property
    def chain_type(self) -> ChainType:
        return ChainType.SOLANA

    @property
    def native_symbol(self) -> str:
        return "SOL"

    @property
    def address_length(self) -> int:
        return 44  # Base58 encoded

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _rpc_call(
        self,
        method: str,
        params: list[Any] | None = None,
    ) -> Any:
        """
        Выполнить JSON-RPC вызов.

        Args:
            method: RPC метод
            params: Параметры

        Returns:
            Результат вызова
        """
        client = await self._get_client()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }

        try:
            response = await client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"Solana RPC error: {data['error']}")
                raise Exception(f"RPC error: {data['error']}")

            return data.get("result")

        except httpx.HTTPError as e:
            logger.error(f"Solana HTTP error: {e}")
            raise

    # === Connection ===

    async def is_connected(self) -> bool:
        """Проверить подключение к ноде."""
        try:
            result = await self._rpc_call("getHealth")
            return result == "ok"
        except Exception:
            return False

    # === Block / Slot ===

    async def get_latest_block(self) -> int:
        """Получить номер последнего слота."""
        result = await self._rpc_call("getSlot")
        return int(result)

    async def get_block_timestamp(self, slot: int) -> int | None:
        """Получить timestamp слота."""
        try:
            result = await self._rpc_call("getBlockTime", [slot])
            return result
        except Exception:
            return None

    # === Balance ===

    async def get_native_balance(self, address: str) -> Decimal:
        """Получить баланс SOL."""
        result = await self._rpc_call("getBalance", [address])
        if result is None:
            return Decimal(0)

        lamports = result.get("value", 0)
        return Decimal(lamports) / Decimal(10**9)

    async def get_token_balance(
        self,
        address: str,
        token_address: str,
    ) -> Decimal:
        """
        Получить баланс SPL токена.

        Args:
            address: Адрес владельца
            token_address: Mint address токена
        """
        # Получаем Associated Token Account (ATA)
        ata = await self._get_associated_token_address(address, token_address)
        if ata is None:
            return Decimal(0)

        # Получаем баланс ATA
        result = await self._rpc_call(
            "getTokenAccountBalance",
            [ata],
        )

        if result is None or "value" not in result:
            return Decimal(0)

        value = result["value"]
        amount = value.get("amount", "0")
        decimals = value.get("decimals", 6)

        return Decimal(amount) / Decimal(10**decimals)

    async def _get_associated_token_address(
        self,
        owner: str,
        mint: str,
    ) -> str | None:
        """
        Вычислить Associated Token Account (ATA) адрес.

        Использует PDA derivation: seeds = [owner, TOKEN_PROGRAM_ID, mint]
        """
        # Запрашиваем ATA через getTokenAccountsByOwner
        result = await self._rpc_call(
            "getTokenAccountsByOwner",
            [
                owner,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        )

        if result is None or "value" not in result:
            return None

        accounts = result["value"]
        if not accounts:
            return None

        # Возвращаем первый найденный аккаунт
        return accounts[0]["pubkey"]

    async def get_all_token_balances(
        self,
        address: str,
        token_addresses: list[str],
    ) -> dict[str, Decimal]:
        """
        Получить балансы всех SPL токенов владельца.

        Оптимизированный запрос через getTokenAccountsByOwner.
        """
        result = await self._rpc_call(
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": SPL_TOKEN_PROGRAM_ID},
                {"encoding": "jsonParsed"},
            ],
        )

        balances: dict[str, Decimal] = {addr: Decimal(0) for addr in token_addresses}

        if result is None or "value" not in result:
            return balances

        for account in result["value"]:
            info = account.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            mint = info.get("mint", "")
            token_amount = info.get("tokenAmount", {})

            if mint in balances:
                amount = token_amount.get("amount", "0")
                decimals = token_amount.get("decimals", 6)
                balances[mint] = Decimal(amount) / Decimal(10**decimals)

        return balances

    # === Transfer Events ===

    async def get_transfer_events(
        self,
        from_slot: int,
        to_slot: int,
        to_addresses: list[str],
        token_addresses: list[str] | None = None,
    ) -> list[TransferEvent]:
        """
        Получить Transfer события за диапазон слотов.

        Solana не имеет прямого аналога getLogs, поэтому:
        1. Получаем транзакции для каждого адреса
        2. Парсим SPL Token transfers
        """
        events: list[TransferEvent] = []

        for address in to_addresses:
            # Получаем signatures для адреса
            signatures = await self._rpc_call(
                "getSignaturesForAddress",
                [
                    address,
                    {
                        "minContextSlot": from_slot,
                        "limit": 1000,
                    },
                ],
            )

            if not signatures:
                continue

            # Фильтруем по слотам
            filtered_sigs = [
                sig for sig in signatures
                if from_slot <= sig.get("slot", 0) <= to_slot
            ]

            # Получаем детали транзакций
            for sig_info in filtered_sigs:
                signature = sig_info["signature"]
                slot = sig_info.get("slot", 0)
                block_time = sig_info.get("blockTime")

                # Получаем полную транзакцию
                tx = await self._rpc_call(
                    "getTransaction",
                    [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )

                if tx is None:
                    continue

                # Парсим SPL Token transfers
                transfers = self._parse_token_transfers(
                    tx, signature, slot, block_time, address, token_addresses
                )
                events.extend(transfers)

        return events

    def _parse_token_transfers(
        self,
        tx: dict,
        signature: str,
        slot: int,
        block_time: int | None,
        target_address: str,
        token_filter: list[str] | None,
    ) -> list[TransferEvent]:
        """Парсить SPL Token transfers из транзакции."""
        events: list[TransferEvent] = []

        meta = tx.get("meta", {})
        if meta is None:
            return events

        # Pre/Post token balances
        pre_balances = meta.get("preTokenBalances", []) or []
        post_balances = meta.get("postTokenBalances", []) or []

        # Inner instructions содержат parsed transfers
        inner_instructions = meta.get("innerInstructions", []) or []

        log_index = 0

        for inner in inner_instructions:
            for ix in inner.get("instructions", []):
                parsed = ix.get("parsed")
                if not parsed:
                    continue

                ix_type = parsed.get("type", "")
                info = parsed.get("info", {})

                if ix_type in ("transfer", "transferChecked"):
                    # SPL Token transfer
                    source = info.get("source", "")
                    destination = info.get("destination", "")
                    authority = info.get("authority", "")

                    # Получаем mint из destination account
                    mint = info.get("mint", "")
                    if not mint:
                        # Попробуем найти в post balances
                        for bal in post_balances:
                            if bal.get("owner") == target_address:
                                mint = bal.get("mint", "")
                                break

                    # Фильтруем по токенам
                    if token_filter and mint not in token_filter:
                        continue

                    # Получаем сумму
                    if ix_type == "transferChecked":
                        token_amount = info.get("tokenAmount", {})
                        raw_amount = int(token_amount.get("amount", 0))
                        decimals = token_amount.get("decimals", 6)
                    else:
                        raw_amount = int(info.get("amount", 0))
                        decimals = self._decimals_cache.get(mint, 6)

                    # Определяем получателя (owner аккаунта)
                    to_owner = ""
                    for bal in post_balances:
                        account = bal.get("account", "")
                        # TODO: более точное сопоставление
                        if bal.get("mint") == mint:
                            to_owner = bal.get("owner", "")

                    if not to_owner:
                        to_owner = target_address

                    # Проверяем, что transfer идёт на наш адрес
                    if to_owner.lower() != target_address.lower():
                        continue

                    token_symbol = self._get_token_symbol(mint)

                    events.append(TransferEvent(
                        tx_hash=signature,
                        block_number=slot,
                        log_index=log_index,
                        from_address=authority or source,
                        to_address=to_owner,
                        token_address=mint,
                        token_symbol=token_symbol,
                        amount=Decimal(raw_amount) / Decimal(10**decimals),
                        raw_amount=raw_amount,
                        chain=self.chain,
                        chain_type=ChainType.SOLANA,
                        timestamp=block_time,
                    ))

                    log_index += 1

        return events

    def _get_token_symbol(self, mint: str) -> str:
        """Получить символ токена по mint address."""
        for token in self.config.tokens.values():
            if token.contract_address == mint:
                return token.symbol
        return "UNKNOWN"

    # === Transaction Status ===

    async def get_confirmations(self, signature: str) -> int | None:
        """Получить количество подтверждений транзакции."""
        result = await self._rpc_call(
            "getSignatureStatuses",
            [[signature]],
        )

        if result is None or "value" not in result:
            return None

        status = result["value"][0]
        if status is None:
            return None

        # Finalized = 32+ confirmations
        confirmation_status = status.get("confirmationStatus", "")
        if confirmation_status == "finalized":
            return 32

        return status.get("confirmations", 0)

    async def is_tx_confirmed(
        self,
        signature: str,
        required_confirmations: int | None = None,
    ) -> bool:
        """Проверить, подтверждена ли транзакция."""
        if required_confirmations is None:
            required_confirmations = self.config.confirmations

        result = await self._rpc_call(
            "getSignatureStatuses",
            [[signature]],
        )

        if result is None or "value" not in result:
            return False

        status = result["value"][0]
        if status is None:
            return False

        # Finalized = полностью подтверждена
        if status.get("confirmationStatus") == "finalized":
            return True

        confirmations = status.get("confirmations", 0) or 0
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
        # Базовая комиссия Solana = 5000 lamports = 0.000005 SOL
        # Для SPL tokens может быть выше из-за создания ATA

        base_fee = 5000  # lamports

        if token_address:
            # Проверяем, существует ли ATA получателя
            ata = await self._get_associated_token_address(to_address, token_address)
            if ata is None:
                # Нужно создать ATA, это стоит ~0.002 SOL rent
                base_fee += 2_000_000  # ~0.002 SOL

        # Priority fee (опционально)
        priority_fee = await self._get_priority_fee()

        total_fee = base_fee + priority_fee

        return FeeEstimate(
            estimated_fee=Decimal(total_fee) / Decimal(10**9),
            estimated_fee_raw=total_fee,
            priority_fee=Decimal(priority_fee) / Decimal(10**9),
        )

    async def _get_priority_fee(self) -> int:
        """Получить рекомендуемую priority fee."""
        try:
            result = await self._rpc_call("getRecentPrioritizationFees", [])
            if result:
                # Берём медианное значение
                fees = [f.get("prioritizationFee", 0) for f in result]
                fees.sort()
                return fees[len(fees) // 2] if fees else 0
        except Exception:
            pass
        return 0

    async def send_native_token(
        self,
        from_private_key: str,
        to_address: str,
        amount: Decimal,
    ) -> TransactionResult:
        """
        Отправить SOL.

        Note: Требует solana-py или solders для подписи.
        Для MVP используем внешний вызов или заглушку.
        """
        # TODO: Реализовать с solana-py
        logger.warning("Solana native transfer not implemented yet")
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
        Отправить SPL токен.

        Note: Требует solana-py или solders для подписи.
        """
        # TODO: Реализовать с solana-py
        logger.warning("Solana token transfer not implemented yet")
        return TransactionResult(
            tx_hash="",
            success=False,
            error="Not implemented",
        )

    # === Address Validation ===

    def is_valid_address(self, address: str) -> bool:
        """Проверить валидность Solana адреса (Base58)."""
        try:
            decoded = base58.b58decode(address)
            return len(decoded) == 32
        except Exception:
            return False

    def normalize_address(self, address: str) -> str:
        """Нормализовать Solana адрес."""
        # Solana адреса case-sensitive, просто проверяем валидность
        if not self.is_valid_address(address):
            raise ValueError(f"Invalid Solana address: {address}")
        return address

    async def close(self) -> None:
        """Закрыть HTTP клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# Singleton адаптер
_solana_adapter: SolanaAdapter | None = None


def get_solana_adapter() -> SolanaAdapter:
    """Получить singleton Solana адаптер."""
    global _solana_adapter
    if _solana_adapter is None:
        _solana_adapter = SolanaAdapter()
    return _solana_adapter
