"""
EVM Adapter - унифицированный интерфейс для работы с EVM блокчейнами.
Поддерживает Base, Arbitrum, BSC и другие EVM-совместимые сети.
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import AsyncWeb3
from web3.exceptions import TransactionNotFound
from web3.types import BlockData, LogReceipt, TxData, TxReceipt

from src.blockchain.chains import (
    ERC20_ABI,
    TRANSFER_EVENT_SIGNATURE,
    ChainConfig,
    get_chain_config,
    get_token_contract,
    get_token_decimals,
    parse_token_amount,
    to_raw_amount,
)
from src.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class TransferLog:
    """Распарсенный Transfer event."""

    tx_hash: str
    log_index: int
    block_number: int
    from_address: str
    to_address: str
    token_contract: str
    amount: Decimal  # Human-readable amount
    raw_amount: int  # Raw amount в smallest units


@dataclass
class FeeParams:
    """Параметры gas fee."""

    # EIP-1559
    max_fee_per_gas: int | None = None
    max_priority_fee_per_gas: int | None = None
    # Legacy
    gas_price: int | None = None
    # Флаг типа
    is_eip1559: bool = True


class EvmAdapter:
    """
    Адаптер для работы с EVM блокчейном.
    Предоставляет унифицированный интерфейс для всех операций.
    """

    # Multicall3 контракт (одинаковый адрес на всех EVM сетях)
    MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
    MULTICALL3_ABI = [
        {
            "inputs": [
                {
                    "components": [
                        {"name": "target", "type": "address"},
                        {"name": "callData", "type": "bytes"},
                    ],
                    "name": "calls",
                    "type": "tuple[]",
                }
            ],
            "name": "aggregate",
            "outputs": [
                {"name": "blockNumber", "type": "uint256"},
                {"name": "returnData", "type": "bytes[]"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    def __init__(
        self,
        chain: str,
        rpc_url: str | None = None,
        use_rpc_manager: bool = True,
    ) -> None:
        """
        Инициализация адаптера.

        Args:
            chain: Имя сети ('base', 'arbitrum', 'bsc')
            rpc_url: Опциональный RPC URL (если не указан, берётся из конфига)
            use_rpc_manager: Использовать RpcManager для multi-RPC ротации
        """
        self.chain = chain.lower()
        self.config = get_chain_config(self.chain)
        self._rpc_manager: "RpcManager | None" = None
        self._use_rpc_manager = use_rpc_manager

        # Кэш: contract_address -> decimals (для O(1) lookup)
        self._decimals_cache: dict[str, int] = {
            token.contract_address.lower(): token.decimals
            for token in self.config.tokens.values()
        }

        # Получаем RPC URL из настроек если не передан явно
        if rpc_url is None:
            settings = get_settings()
            rpc_urls = {
                "base": settings.base_rpc_url,
                "arbitrum": settings.arb_rpc_url,
                "bsc": settings.bsc_rpc_url,
                "polygon": settings.polygon_rpc_url,
                "avax": settings.avax_rpc_url,
                "optimism": settings.optimism_rpc_url,
            }
            rpc_url = rpc_urls.get(self.chain, self.config.rpc_url)

        self.rpc_url = rpc_url
        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))

        logger.info(
            f"Initialized EvmAdapter for {chain} (chain_id={self.config.chain_id})"
        )

    def set_rpc_manager(self, manager: "RpcManager") -> None:
        """Установить RpcManager для multi-RPC ротации."""
        from src.blockchain.rpc_manager import RpcManager

        self._rpc_manager = manager
        logger.info(f"RpcManager attached to EvmAdapter for {self.chain}")

    async def _get_web3(self) -> AsyncWeb3:
        """Получить Web3 instance (с ротацией если RpcManager настроен)."""
        if self._rpc_manager and self._use_rpc_manager:
            return await self._rpc_manager.get_web3()
        return self.w3

    # === Block Methods ===

    async def get_latest_block_number(self) -> int:
        """Получить номер последнего блока."""
        if self._rpc_manager and self._use_rpc_manager:
            return await self._rpc_manager.execute(lambda w3: w3.eth.block_number)
        return await self.w3.eth.block_number

    async def get_block(
        self,
        block_number: int | str = "latest",
        full_transactions: bool = False,
    ) -> BlockData:
        """
        Получить блок по номеру.

        Args:
            block_number: Номер блока или 'latest'
            full_transactions: Включить полные данные транзакций
        """
        if self._rpc_manager and self._use_rpc_manager:
            return await self._rpc_manager.execute(
                lambda w3: w3.eth.get_block(
                    block_number, full_transactions=full_transactions
                )
            )
        return await self.w3.eth.get_block(
            block_number, full_transactions=full_transactions
        )

    # === Transaction Methods ===

    async def get_transaction(self, tx_hash: str) -> TxData | None:
        """Получить транзакцию по хешу."""
        try:
            return await self.w3.eth.get_transaction(tx_hash)
        except TransactionNotFound:
            return None

    async def get_transaction_receipt(self, tx_hash: str) -> TxReceipt | None:
        """Получить receipt транзакции."""
        try:
            return await self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None

    async def get_confirmations(self, tx_hash: str) -> int | None:
        """
        Получить количество подтверждений транзакции.

        Returns:
            Количество подтверждений или None если транзакция не найдена
        """
        receipt = await self.get_transaction_receipt(tx_hash)
        if receipt is None or receipt.get("blockNumber") is None:
            return None

        latest_block = await self.get_latest_block_number()
        return latest_block - receipt["blockNumber"] + 1

    async def is_tx_confirmed(
        self, tx_hash: str, required_confirmations: int | None = None
    ) -> bool:
        """
        Проверить, подтверждена ли транзакция.

        Args:
            tx_hash: Хеш транзакции
            required_confirmations: Требуемое количество подтверждений (по умолчанию из конфига)
        """
        if required_confirmations is None:
            required_confirmations = self.config.confirmations

        confirmations = await self.get_confirmations(tx_hash)
        if confirmations is None:
            return False

        return confirmations >= required_confirmations

    # === Log Methods ===

    async def get_logs(
        self,
        from_block: int,
        to_block: int,
        address: str | list[str] | None = None,
        topics: list[str | list[str] | None] | None = None,
    ) -> list[LogReceipt]:
        """
        Получить логи событий.

        Args:
            from_block: Начальный блок
            to_block: Конечный блок
            address: Адрес контракта или список адресов
            topics: Топики для фильтрации
        """
        filter_params: dict[str, Any] = {
            "fromBlock": from_block,
            "toBlock": to_block,
        }

        if address is not None:
            # Приводим адреса к checksum формату (web3.py требует)
            if isinstance(address, list):
                filter_params["address"] = [
                    self.w3.to_checksum_address(a) for a in address
                ]
            else:
                filter_params["address"] = self.w3.to_checksum_address(address)

        if topics is not None:
            filter_params["topics"] = topics

        return await self.w3.eth.get_logs(filter_params)

    async def get_transfer_logs(
        self,
        from_block: int,
        to_block: int,
        to_address: str,
        token_contracts: list[str] | None = None,
    ) -> list[TransferLog]:
        """
        Получить Transfer события для указанного адреса получателя.

        Args:
            from_block: Начальный блок
            to_block: Конечный блок
            to_address: Адрес получателя (deposit address)
            token_contracts: Список контрактов токенов для фильтрации
        """
        # Паддинг адреса до 32 байт для topic2
        padded_to = "0x" + to_address[2:].lower().zfill(64)

        # Topics: [Transfer signature, from (any), to (our address)]
        topics: list[str | None] = [
            TRANSFER_EVENT_SIGNATURE,
            None,  # from - любой
            padded_to,  # to - наш адрес
        ]

        # Адреса контрактов для фильтрации
        address_filter: str | list[str] | None = None
        if token_contracts:
            address_filter = token_contracts

        logger.debug(
            f"[{self.chain}] get_transfer_logs: blocks {from_block}-{to_block}, "
            f"to={to_address}, tokens={token_contracts}"
        )

        logs = await self.get_logs(
            from_block=from_block,
            to_block=to_block,
            address=address_filter,
            topics=topics,
        )

        if logs:
            logger.info(f"[{self.chain}] Found {len(logs)} raw logs for {to_address}")

        result: list[TransferLog] = []
        for log in logs:
            try:
                transfer = self._parse_transfer_log(log)
                if transfer:
                    result.append(transfer)
            except Exception as e:
                logger.warning(f"Failed to parse transfer log: {e}")

        return result

    async def get_transfer_logs_batch(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_contracts: list[str] | None = None,
    ) -> list[TransferLog]:
        """
        Получить Transfer события для МНОЖЕСТВА адресов получателей.

        Делает индивидуальные запросы для каждого адреса (надёжно для всех RPC).

        Args:
            from_block: Начальный блок
            to_block: Конечный блок
            to_addresses: Список адресов получателей (deposit addresses)
            token_contracts: Список контрактов токенов для фильтрации
        """
        if not to_addresses:
            return []

        result: list[TransferLog] = []

        # Делаем запросы для каждого адреса отдельно
        # Это надёжнее чем OR в topics (не все RPC поддерживают)
        for to_address in to_addresses:
            try:
                transfers = await self.get_transfer_logs(
                    from_block=from_block,
                    to_block=to_block,
                    to_address=to_address,
                    token_contracts=token_contracts,
                )
                result.extend(transfers)
            except Exception as e:
                logger.warning(f"Error fetching logs for {to_address}: {e}")

        return result

    def _parse_transfer_log(self, log: LogReceipt) -> TransferLog | None:
        """Распарсить Transfer event лог."""
        if len(log["topics"]) < 3:
            return None

        tx_hash = (
            log["transactionHash"].hex()
            if isinstance(log["transactionHash"], bytes)
            else log["transactionHash"]
        )

        # Парсим топики
        from_address = "0x" + log["topics"][1].hex()[-40:]
        to_address = "0x" + log["topics"][2].hex()[-40:]

        # Парсим data (amount)
        raw_amount = (
            int(log["data"].hex(), 16)
            if isinstance(log["data"], bytes)
            else int(log["data"], 16)
        )

        # Определяем токен по адресу контракта
        token_contract = log["address"]
        if isinstance(token_contract, bytes):
            token_contract = token_contract.hex()
        token_contract = token_contract.lower()

        # Получаем decimals из кэша (O(1) вместо O(N))
        decimals = self._decimals_cache.get(token_contract, 18)
        amount = Decimal(raw_amount) / Decimal(10**decimals)

        return TransferLog(
            tx_hash=tx_hash,
            log_index=log["logIndex"],
            block_number=log["blockNumber"],
            from_address=from_address,
            to_address=to_address,
            token_contract=token_contract,
            amount=amount,
            raw_amount=raw_amount,
        )

    # === Balance Methods ===

    async def get_native_balance(self, address: str) -> Decimal:
        """
        Получить баланс нативного токена (ETH/BNB).

        Returns:
            Баланс в ETH/BNB (не в wei)
        """
        balance_wei = await self.w3.eth.get_balance(
            self.w3.to_checksum_address(address)
        )
        return Decimal(balance_wei) / Decimal(10**18)

    async def get_native_balance_wei(self, address: str) -> int:
        """Получить баланс в wei."""
        return await self.w3.eth.get_balance(self.w3.to_checksum_address(address))

    async def get_erc20_balance(self, address: str, token_contract: str) -> Decimal:
        """
        Получить баланс ERC20 токена.

        Args:
            address: Адрес кошелька
            token_contract: Адрес контракта токена

        Returns:
            Баланс в human-readable формате
        """
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_contract),
            abi=ERC20_ABI,
        )
        balance = await contract.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()

        # Получаем decimals из кэша (O(1) вместо O(N))
        decimals = self._decimals_cache.get(token_contract.lower(), 18)
        return Decimal(balance) / Decimal(10**decimals)

    async def get_erc20_balance_raw(self, address: str, token_contract: str) -> int:
        """Получить баланс ERC20 в raw units."""
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_contract),
            abi=ERC20_ABI,
        )
        return await contract.functions.balanceOf(
            self.w3.to_checksum_address(address)
        ).call()

    async def get_balances_batch(
        self,
        addresses: list[str],
        token_contracts: list[str],
    ) -> dict[str, dict[str, Decimal]]:
        """
        Получить балансы нескольких адресов для нескольких токенов за ОДИН RPC вызов.

        Использует Multicall3 контракт для батчинга запросов.

        Args:
            addresses: Список адресов кошельков
            token_contracts: Список адресов контрактов токенов

        Returns:
            Dict[address, Dict[token_contract, balance]]
        """
        if not addresses or not token_contracts:
            return {}

        # Готовим calldata для balanceOf
        erc20_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_contracts[0]),
            abi=ERC20_ABI,
        )

        # Формируем список вызовов: [(target, callData), ...]
        calls: list[tuple[str, bytes]] = []
        call_map: list[tuple[str, str]] = (
            []
        )  # (address, token_contract) для маппинга результатов

        for address in addresses:
            checksum_addr = self.w3.to_checksum_address(address)
            for token_contract in token_contracts:
                calldata = erc20_contract.encode_abi(
                    abi_element_identifier="balanceOf",
                    args=[checksum_addr],
                )
                calls.append((self.w3.to_checksum_address(token_contract), calldata))
                call_map.append((address.lower(), token_contract.lower()))

        # Вызываем Multicall3
        multicall = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.MULTICALL3_ADDRESS),
            abi=self.MULTICALL3_ABI,
        )

        try:
            _, return_data = await multicall.functions.aggregate(calls).call()
        except Exception as e:
            logger.warning(f"Multicall failed, falling back to individual calls: {e}")
            # Fallback на индивидуальные вызовы
            return await self._get_balances_fallback(addresses, token_contracts)

        # Парсим результаты
        result: dict[str, dict[str, Decimal]] = {}
        for i, data in enumerate(return_data):
            address, token_contract = call_map[i]
            raw_balance = int(data.hex(), 16) if data else 0
            decimals = self._decimals_cache.get(token_contract, 18)
            balance = Decimal(raw_balance) / Decimal(10**decimals)

            if address not in result:
                result[address] = {}
            result[address][token_contract] = balance

        return result

    async def _get_balances_fallback(
        self,
        addresses: list[str],
        token_contracts: list[str],
    ) -> dict[str, dict[str, Decimal]]:
        """Fallback для получения балансов без Multicall."""
        result: dict[str, dict[str, Decimal]] = {}
        for address in addresses:
            result[address.lower()] = {}
            for token_contract in token_contracts:
                try:
                    balance = await self.get_erc20_balance(address, token_contract)
                    result[address.lower()][token_contract.lower()] = balance
                except Exception:
                    result[address.lower()][token_contract.lower()] = Decimal(0)
        return result

    async def get_native_balances_batch(
        self, addresses: list[str]
    ) -> dict[str, Decimal]:
        """
        Получить native балансы нескольких адресов за ОДИН RPC вызов.

        Использует Multicall3 с eth_getBalance.

        Args:
            addresses: Список адресов

        Returns:
            Dict[address, balance]
        """
        if not addresses:
            return {}

        # Multicall3 имеет getEthBalance функцию
        multicall3_with_eth = [
            {
                "inputs": [{"name": "addr", "type": "address"}],
                "name": "getEthBalance",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            },
        ] + self.MULTICALL3_ABI

        multicall = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.MULTICALL3_ADDRESS),
            abi=multicall3_with_eth,
        )

        # Формируем вызовы getEthBalance
        calls: list[tuple[str, bytes]] = []
        for address in addresses:
            calldata = multicall.encode_abi(
                abi_element_identifier="getEthBalance",
                args=[self.w3.to_checksum_address(address)],
            )
            calls.append((self.MULTICALL3_ADDRESS, calldata))

        try:
            _, return_data = await multicall.functions.aggregate(calls).call()
        except Exception as e:
            logger.warning(f"Multicall for native balances failed: {e}")
            # Fallback
            result = {}
            for address in addresses:
                try:
                    result[address.lower()] = await self.get_native_balance(address)
                except Exception:
                    result[address.lower()] = Decimal(0)
            return result

        # Парсим результаты
        result: dict[str, Decimal] = {}
        for i, data in enumerate(return_data):
            raw_balance = int(data.hex(), 16) if data else 0
            result[addresses[i].lower()] = Decimal(raw_balance) / Decimal(10**18)

        return result

    # === Transaction Building ===

    async def get_priority_fee_floor(self) -> int | None:
        """
        Получить динамический минимум priority fee из fee history.

        Returns:
            Максимальная reward за последние блоки или None при ошибке.
        """
        try:
            fee_history = await self.w3.eth.fee_history(5, "latest", [50])
            rewards = fee_history.get("reward", [])
            flat_rewards = [reward for batch in rewards for reward in batch if reward]
            if flat_rewards:
                return max(flat_rewards)
        except Exception as e:
            logger.debug(f"Failed to get fee history: {e}")
        return None

    async def get_fee_params(self) -> FeeParams:
        """
        Получить текущие параметры gas fee.
        Автоматически определяет EIP-1559 или legacy.
        """
        try:
            # Пробуем EIP-1559
            latest_block = await self.get_block("latest")
            if "baseFeePerGas" in latest_block:
                base_fee = latest_block["baseFeePerGas"]
                # Приоритетная fee (берем max из max_priority_fee и fee history)
                max_priority = await self.w3.eth.max_priority_fee
                priority_floor = await self.get_priority_fee_floor()
                if priority_floor is not None and priority_floor > max_priority:
                    max_priority = priority_floor
                # Максимальная fee = base * 2 + priority
                max_fee = base_fee * 2 + max_priority

                return FeeParams(
                    max_fee_per_gas=max_fee,
                    max_priority_fee_per_gas=max_priority,
                    is_eip1559=True,
                )
        except Exception:
            pass

        # Fallback на legacy
        gas_price = await self.w3.eth.gas_price
        return FeeParams(
            gas_price=gas_price,
            is_eip1559=False,
        )

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        """Оценить gas для транзакции."""
        return await self.w3.eth.estimate_gas(tx)

    async def get_nonce(self, address: str) -> int:
        """Получить nonce для адреса."""
        return await self.w3.eth.get_transaction_count(
            self.w3.to_checksum_address(address)
        )

    async def build_erc20_transfer_tx(
        self,
        from_address: str,
        to_address: str,
        token_contract: str,
        amount: Decimal,
        token_symbol: str,
    ) -> dict[str, Any]:
        """
        Построить транзакцию ERC20 transfer.

        Args:
            from_address: Адрес отправителя
            to_address: Адрес получателя
            token_contract: Адрес контракта токена
            amount: Сумма в human-readable формате
            token_symbol: Символ токена ('USDT', 'USDC')

        Returns:
            Готовая транзакция для подписи
        """
        # Конвертируем amount в raw
        raw_amount = to_raw_amount(amount, self.chain, token_symbol)

        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_contract),
            abi=ERC20_ABI,
        )

        # Получаем fee params
        fee_params = await self.get_fee_params()

        # Строим транзакцию
        tx: dict[str, Any] = {
            "chainId": self.config.chain_id,
            "from": self.w3.to_checksum_address(from_address),
            "nonce": await self.get_nonce(from_address),
        }

        if fee_params.is_eip1559:
            tx["maxFeePerGas"] = fee_params.max_fee_per_gas
            tx["maxPriorityFeePerGas"] = fee_params.max_priority_fee_per_gas
        else:
            tx["gasPrice"] = fee_params.gas_price

        # Строим data для transfer
        tx["to"] = self.w3.to_checksum_address(token_contract)
        tx["data"] = contract.encode_abi(
            abi_element_identifier="transfer",
            args=[self.w3.to_checksum_address(to_address), raw_amount],
        )
        tx["value"] = 0

        # Оценка gas
        gas_estimate = await self.estimate_gas(tx)
        tx["gas"] = int(gas_estimate * 1.2)  # 20% buffer

        return tx

    async def build_native_transfer_tx(
        self,
        from_address: str,
        to_address: str,
        amount_wei: int,
    ) -> dict[str, Any]:
        """
        Построить транзакцию отправки нативного токена.

        Args:
            from_address: Адрес отправителя
            to_address: Адрес получателя
            amount_wei: Сумма в wei
        """
        fee_params = await self.get_fee_params()

        tx: dict[str, Any] = {
            "chainId": self.config.chain_id,
            "from": self.w3.to_checksum_address(from_address),
            "to": self.w3.to_checksum_address(to_address),
            "value": amount_wei,
            "nonce": await self.get_nonce(from_address),
        }

        if fee_params.is_eip1559:
            tx["maxFeePerGas"] = fee_params.max_fee_per_gas
            tx["maxPriorityFeePerGas"] = fee_params.max_priority_fee_per_gas
        else:
            tx["gasPrice"] = fee_params.gas_price

        # Gas для простого перевода
        tx["gas"] = 21000

        return tx

    # === Transaction Sending ===

    async def send_raw_transaction(self, signed_tx: bytes) -> str:
        """
        Отправить подписанную транзакцию.

        Returns:
            Transaction hash
        """
        tx_hash = await self.w3.eth.send_raw_transaction(signed_tx)
        return tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash

    async def sign_and_send_transaction(
        self,
        tx: dict[str, Any],
        private_key: str,
    ) -> str:
        """
        Подписать и отправить транзакцию.

        Args:
            tx: Транзакция
            private_key: Приватный ключ (hex)

        Returns:
            Transaction hash
        """
        signed = Account.sign_transaction(tx, private_key)
        return await self.send_raw_transaction(signed.raw_transaction)

    # === High-Level Transfer Methods ===

    async def send_native_transfer(
        self,
        from_private_key: str,
        to_address: str,
        amount_wei: int,
        max_retries: int = 3,
    ) -> str | None:
        """
        Отправить нативный токен (ETH/BNB).

        Args:
            from_private_key: Приватный ключ отправителя
            to_address: Адрес получателя
            amount_wei: Сумма в wei
            max_retries: Максимум повторных попыток при ошибке

        Returns:
            Transaction hash или None при ошибке
        """
        account = Account.from_key(from_private_key)
        last_error = None

        for attempt in range(max_retries):
            try:
                # Получаем свежие fee params и nonce на каждой попытке
                fee_params = await self.get_fee_params()
                nonce = await self.get_nonce(account.address)

                tx: dict[str, Any] = {
                    "chainId": self.config.chain_id,
                    "from": self.w3.to_checksum_address(account.address),
                    "to": self.w3.to_checksum_address(to_address),
                    "value": amount_wei,
                    "nonce": nonce,
                    "gas": 21000,
                }

                if fee_params.is_eip1559:
                    tx["maxFeePerGas"] = int(fee_params.max_fee_per_gas * 1.2)
                    tx["maxPriorityFeePerGas"] = fee_params.max_priority_fee_per_gas
                else:
                    tx["gasPrice"] = int(fee_params.gas_price * 1.1)

                return await self.sign_and_send_transaction(tx, from_private_key)
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                # Retry на ошибки связанные с nonce или ценой газа
                if (
                    "nonce" in error_str
                    or "base fee" in error_str
                    or "gas" in error_str
                ):
                    logger.warning(
                        f"[{self.chain}] Native transfer attempt {attempt + 1}/{max_retries} failed: {e}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                break

        logger.error(f"Failed to send native transfer: {last_error}")
        return None

    async def send_erc20_transfer(
        self,
        from_private_key: str,
        token_contract: str,
        to_address: str,
        amount: int | Decimal,
        token_symbol: str | None = None,
        max_retries: int = 3,
    ) -> str | None:
        """
        Отправить ERC20 токен.

        Args:
            from_private_key: Приватный ключ отправителя
            token_contract: Адрес контракта токена
            to_address: Адрес получателя
            amount: Сумма (raw если int, human-readable если Decimal с token_symbol)
            token_symbol: Символ токена (нужен если amount в human-readable)
            max_retries: Максимум повторных попыток при ошибке fee

        Returns:
            Transaction hash или None при ошибке
        """
        account = Account.from_key(from_private_key)

        # Если amount - int, считаем что это raw amount
        if isinstance(amount, int):
            raw_amount = amount
        else:
            if not token_symbol:
                # Пытаемся найти symbol по контракту
                for sym, token_config in self.config.tokens.items():
                    if token_config.contract_address.lower() == token_contract.lower():
                        token_symbol = sym
                        break
                else:
                    raise ValueError("token_symbol required for Decimal amount")
            raw_amount = to_raw_amount(amount, self.chain, token_symbol)

        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_contract),
            abi=ERC20_ABI,
        )

        last_error = None
        for attempt in range(max_retries):
            try:
                # Получаем свежие fee params на каждой попытке
                fee_params = await self.get_fee_params()

                tx: dict[str, Any] = {
                    "chainId": self.config.chain_id,
                    "from": self.w3.to_checksum_address(account.address),
                    "nonce": await self.get_nonce(account.address),
                    "to": self.w3.to_checksum_address(token_contract),
                    "data": contract.encode_abi(
                        abi_element_identifier="transfer",
                        args=[self.w3.to_checksum_address(to_address), raw_amount],
                    ),
                    "value": 0,
                }

                if fee_params.is_eip1559:
                    # Добавляем буфер 20% к maxFeePerGas для волатильных сетей
                    tx["maxFeePerGas"] = int(fee_params.max_fee_per_gas * 1.2)
                    tx["maxPriorityFeePerGas"] = fee_params.max_priority_fee_per_gas
                else:
                    # BSC требует более высокий gas price из-за волатильности
                    gas_multiplier = 1.5 if self.chain == "bsc" else 1.2
                    tx["gasPrice"] = int(fee_params.gas_price * gas_multiplier)

                gas_estimate = await self.estimate_gas(tx)
                tx["gas"] = int(gas_estimate * 1.3)  # 30% buffer for gas limit

                return await self.sign_and_send_transaction(tx, from_private_key)
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                # Retry на ошибки связанные с ценой газа
                if "base fee" in error_str or "gas" in error_str:
                    logger.warning(
                        f"[{self.chain}] ERC20 transfer attempt {attempt + 1}/{max_retries} failed: {e}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)  # Небольшая пауза перед retry
                        continue
                break

        logger.error(f"Failed to send ERC20 transfer: {last_error}")
        return None

    async def estimate_gas_for_erc20_transfer(
        self,
        token_contract: str,
        from_address: str,
        to_address: str,
        amount: int,
    ) -> int | None:
        """
        Оценить gas для ERC20 transfer.

        Args:
            token_contract: Адрес контракта токена
            from_address: Адрес отправителя
            to_address: Адрес получателя
            amount: Сумма в raw units

        Returns:
            Gas estimate или None при ошибке
        """
        try:
            contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(token_contract),
                abi=ERC20_ABI,
            )

            tx = {
                "from": self.w3.to_checksum_address(from_address),
                "to": self.w3.to_checksum_address(token_contract),
                "data": contract.encode_abi(
                    abi_element_identifier="transfer",
                    args=[self.w3.to_checksum_address(to_address), amount],
                ),
                "value": 0,
            }

            return await self.estimate_gas(tx)
        except Exception as e:
            logger.warning(f"Failed to estimate gas: {e}")
            return None

    async def get_gas_price(self) -> int | None:
        """Получить текущую цену газа в wei."""
        try:
            return await self.w3.eth.gas_price
        except Exception as e:
            logger.warning(f"Failed to get gas price: {e}")
            return None

    def private_key_to_address(self, private_key: str) -> str:
        """Получить адрес из приватного ключа."""
        account = Account.from_key(private_key)
        return account.address

    # === Utility ===

    def to_checksum_address(self, address: str) -> str:
        """Преобразовать адрес в checksum формат."""
        return self.w3.to_checksum_address(address)

    async def is_connected(self) -> bool:
        """Проверить соединение с RPC."""
        return await self.w3.is_connected()

    async def close(self) -> None:
        """Закрыть соединение и освободить ресурсы."""
        try:
            # AsyncHTTPProvider хранит session внутри
            provider = self.w3.provider
            if hasattr(provider, "_session") and provider._session is not None:
                if not provider._session.closed:
                    await provider._session.close()
            # Также проверяем _request_session (зависит от версии web3)
            if (
                hasattr(provider, "_request_session")
                and provider._request_session is not None
            ):
                if not provider._request_session.closed:
                    await provider._request_session.close()
        except Exception as e:
            logger.warning(f"Error closing EvmAdapter for {self.chain}: {e}")


# Глобальный кэш адаптеров (без lru_cache для поддержки async close)
_adapter_cache: dict[str, EvmAdapter] = {}


def normalize_chain_name(chain: str) -> str:
    """
    Нормализовать имя сети (алиасы -> каноническое имя).

    Args:
        chain: Имя сети или алиас ('arb', 'opt', 'bnb')

    Returns:
        Каноническое имя сети ('arbitrum', 'optimism', 'bsc')
    """
    aliases = {
        "arb": "arbitrum",
        "bnb": "bsc",
        "opt": "optimism",
    }
    return aliases.get(chain.lower(), chain.lower())


def get_evm_adapter(chain: str) -> EvmAdapter:
    """
    Получить кешированный экземпляр EvmAdapter.

    Args:
        chain: Имя сети ('base', 'arbitrum', 'bsc') или алиас ('arb', 'opt', 'bnb')
    """
    normalized = normalize_chain_name(chain)
    if normalized not in _adapter_cache:
        _adapter_cache[normalized] = EvmAdapter(normalized)
    return _adapter_cache[normalized]


async def close_all_adapters() -> None:
    """Закрыть все кэшированные адаптеры."""
    for chain, adapter in _adapter_cache.items():
        await adapter.close()
    _adapter_cache.clear()
    logger.info("All EvmAdapter instances closed")
