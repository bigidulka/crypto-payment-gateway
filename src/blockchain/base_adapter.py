"""
Base Adapter - абстрактный интерфейс для всех блокчейн адаптеров.

Поддерживает:
- EVM сети (Ethereum, BSC, Base, Arbitrum, etc.)
- Solana
- TON

Каждый адаптер реализует этот интерфейс для своего типа сети.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Generic, TypeVar


class ChainType(str, Enum):
    """Тип блокчейн сети."""

    EVM = "evm"
    SOLANA = "solana"
    TON = "ton"


@dataclass
class TransferEvent:
    """
    Универсальный Transfer event для всех сетей.
    Формат унифицирован для работы с любым блокчейном.
    """

    # Идентификация транзакции
    tx_hash: str  # Хеш транзакции (hex для EVM, base58 для Solana, etc.)
    block_number: int  # Номер блока/слота
    log_index: int  # Индекс события в блоке (для уникальности)

    # Адреса
    from_address: str  # Отправитель
    to_address: str  # Получатель

    # Токен
    token_address: str  # Адрес контракта/mint (пустой для нативного токена)
    token_symbol: str  # Символ токена (USDT, USDC, SOL, TON)

    # Сумма
    amount: Decimal  # Human-readable amount
    raw_amount: int  # Raw amount в smallest units

    # Метаданные
    chain: str  # Имя сети (base, solana, ton)
    chain_type: ChainType  # Тип сети
    timestamp: int | None = None  # Unix timestamp блока


@dataclass
class BalanceInfo:
    """Информация о балансе."""

    address: str
    token_address: str  # Пустой для нативного токена
    token_symbol: str
    balance: Decimal  # Human-readable
    raw_balance: int  # Raw amount
    chain: str


@dataclass
class TransactionResult:
    """Результат отправки транзакции."""

    tx_hash: str
    success: bool
    error: str | None = None
    gas_used: int | None = None
    fee_paid: Decimal | None = None


@dataclass
class FeeEstimate:
    """Оценка комиссии за транзакцию."""

    estimated_fee: Decimal  # В нативном токене
    estimated_fee_raw: int
    priority_fee: Decimal | None = None  # Для Solana priority fees
    gas_limit: int | None = None  # Для EVM


# TypeVar для типизации конфигурации
ConfigT = TypeVar("ConfigT")


class BaseAdapter(ABC, Generic[ConfigT]):
    """
    Абстрактный базовый адаптер для блокчейнов.

    Определяет унифицированный интерфейс для:
    - Получения балансов
    - Отслеживания транзакций (Transfer events)
    - Отправки транзакций (sweep)
    - Генерации адресов
    """

    def __init__(self, chain: str, config: ConfigT) -> None:
        self.chain = chain
        self.config = config

    @property
    @abstractmethod
    def chain_type(self) -> ChainType:
        """Тип блокчейна."""
        ...

    @property
    @abstractmethod
    def native_symbol(self) -> str:
        """Символ нативного токена (ETH, SOL, TON)."""
        ...

    @property
    @abstractmethod
    def address_length(self) -> int:
        """Длина адреса в символах."""
        ...

    # === Connection ===

    @abstractmethod
    async def is_connected(self) -> bool:
        """Проверить подключение к ноде."""
        ...

    # === Block / Slot ===

    @abstractmethod
    async def get_latest_block(self) -> int:
        """Получить номер последнего блока/слота."""
        ...

    @abstractmethod
    async def get_block_timestamp(self, block: int) -> int | None:
        """Получить timestamp блока."""
        ...

    # === Balance ===

    @abstractmethod
    async def get_native_balance(self, address: str) -> Decimal:
        """Получить баланс нативного токена."""
        ...

    @abstractmethod
    async def get_token_balance(
        self,
        address: str,
        token_address: str,
    ) -> Decimal:
        """Получить баланс токена."""
        ...

    async def get_all_token_balances(
        self,
        address: str,
        token_addresses: list[str],
    ) -> dict[str, Decimal]:
        """
        Получить балансы всех токенов.
        По умолчанию делает последовательные запросы.
        Переопределить для batch-запросов.
        """
        result = {}
        for token in token_addresses:
            result[token] = await self.get_token_balance(address, token)
        return result

    # === Transfer Events ===

    @abstractmethod
    async def get_transfer_events(
        self,
        from_block: int,
        to_block: int,
        to_addresses: list[str],
        token_addresses: list[str] | None = None,
    ) -> list[TransferEvent]:
        """
        Получить Transfer события за диапазон блоков.

        Args:
            from_block: Начальный блок/слот
            to_block: Конечный блок/слот
            to_addresses: Адреса получателей для фильтрации
            token_addresses: Опционально - адреса токенов для фильтрации

        Returns:
            Список TransferEvent
        """
        ...

    # === Transaction Status ===

    @abstractmethod
    async def get_confirmations(self, tx_hash: str) -> int | None:
        """
        Получить количество подтверждений транзакции.

        Returns:
            Количество подтверждений или None если транзакция не найдена
        """
        ...

    @abstractmethod
    async def is_tx_confirmed(
        self,
        tx_hash: str,
        required_confirmations: int | None = None,
    ) -> bool:
        """Проверить, подтверждена ли транзакция."""
        ...

    # === Send Transaction ===

    @abstractmethod
    async def estimate_transfer_fee(
        self,
        from_address: str,
        to_address: str,
        token_address: str | None = None,
        amount: Decimal | None = None,
    ) -> FeeEstimate:
        """Оценить комиссию за transfer транзакцию."""
        ...

    @abstractmethod
    async def send_native_token(
        self,
        from_private_key: str,
        to_address: str,
        amount: Decimal,
    ) -> TransactionResult:
        """Отправить нативный токен."""
        ...

    @abstractmethod
    async def send_token(
        self,
        from_private_key: str,
        to_address: str,
        token_address: str,
        amount: Decimal,
    ) -> TransactionResult:
        """Отправить токен (ERC20/SPL/Jetton)."""
        ...

    # === Address Validation ===

    @abstractmethod
    def is_valid_address(self, address: str) -> bool:
        """Проверить валидность адреса."""
        ...

    @abstractmethod
    def normalize_address(self, address: str) -> str:
        """
        Нормализовать адрес к стандартному формату.
        EVM: checksum, Solana: base58, TON: user-friendly
        """
        ...

    # === Utility ===

    def parse_amount(self, raw_amount: int, decimals: int) -> Decimal:
        """Преобразовать raw amount в human-readable."""
        return Decimal(raw_amount) / Decimal(10**decimals)

    def to_raw_amount(self, amount: Decimal, decimals: int) -> int:
        """Преобразовать human-readable в raw amount."""
        return int(amount * Decimal(10**decimals))


def get_adapter(chain: str) -> BaseAdapter:
    """
    Фабрика для получения адаптера по имени сети.

    Определяет тип сети и возвращает соответствующий адаптер.
    """
    from src.blockchain.chains import get_chain_config

    config = get_chain_config(chain)

    # Определяем тип сети
    chain_lower = chain.lower()

    if chain_lower == "solana":
        from src.blockchain.solana_adapter import SolanaAdapter

        return SolanaAdapter(chain, config)

    if chain_lower == "ton":
        from src.blockchain.ton_adapter import TonAdapter

        return TonAdapter(chain, config)

    # По умолчанию - EVM
    from src.blockchain.evm_adapter import EvmAdapter

    return EvmAdapter(chain)
