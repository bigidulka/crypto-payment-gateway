"""
Конфигурация блокчейн сетей и токенов.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Literal

# Типы для сетей и токенов
ChainName = Literal["base", "arbitrum", "bsc", "polygon", "avax", "optimism"]
TokenSymbol = Literal["USDT", "USDC"]


@dataclass(frozen=True)
class TokenConfig:
    """Конфигурация токена."""

    symbol: str
    contract_address: str
    decimals: int = 18
    variant: str = "native"  # native | bridged | wrapped
    bridge: str | None = None


@dataclass(frozen=True)
class ChainConfig:
    """Конфигурация блокчейн сети."""

    name: str
    chain_id: int
    rpc_url: str  # Будет переопределён из env
    confirmations: int  # Требуемое количество подтверждений
    reorg_buffer: int  # Буфер для защиты от реорганизаций
    scan_window: int  # Размер окна сканирования блоков
    block_time_sec: float  # Примерное время блока в секундах
    native_symbol: str  # Нативный токен (ETH/BNB)
    explorer_url: str  # URL блок-эксплорера
    tokens: dict[str, TokenConfig] = field(default_factory=dict)
    treasury_address: str = ""  # Будет переопределён из env

    def get_token(self, symbol: str) -> TokenConfig | None:
        """Получить конфигурацию токена по символу."""
        return self.tokens.get(symbol.upper())

    def get_explorer_tx_url(self, tx_hash: str) -> str:
        """Получить URL транзакции в эксплорере."""
        return f"{self.explorer_url}/tx/{tx_hash}"

    def get_explorer_address_url(self, address: str) -> str:
        """Получить URL адреса в эксплорере."""
        return f"{self.explorer_url}/address/{address}"


# Дефолтные конфигурации сетей
# RPC URL и treasury будут переопределены из настроек

CHAINS_CONFIG: dict[str, ChainConfig] = {
    "base": ChainConfig(
        name="Base",
        chain_id=8453,
        rpc_url="https://mainnet.base.org",
        confirmations=12,
        reorg_buffer=20,
        scan_window=2000,
        block_time_sec=2.0,
        native_symbol="ETH",
        explorer_url="https://basescan.org",
        tokens={
            "USDT": TokenConfig(
                symbol="USDT",
                contract_address="0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
                decimals=6,
                variant="bridged",
            ),
            "USDC": TokenConfig(
                symbol="USDC",
                contract_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                decimals=6,
            ),
        },
    ),
    "arbitrum": ChainConfig(
        name="Arbitrum One",
        chain_id=42161,
        rpc_url="https://arb1.arbitrum.io/rpc",
        confirmations=12,
        reorg_buffer=50,
        scan_window=2000,
        block_time_sec=0.25,
        native_symbol="ETH",
        explorer_url="https://arbiscan.io",
        tokens={
            "USDT": TokenConfig(
                symbol="USDT",
                contract_address="0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
                decimals=6,
            ),
            "USDC": TokenConfig(
                symbol="USDC",
                contract_address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
                decimals=6,
            ),
        },
    ),
    "bsc": ChainConfig(
        name="BNB Smart Chain",
        chain_id=56,
        rpc_url="https://bsc-dataseed.binance.org",
        confirmations=15,
        reorg_buffer=30,
        scan_window=2000,
        block_time_sec=3.0,
        native_symbol="BNB",
        explorer_url="https://bscscan.com",
        tokens={
            "USDT": TokenConfig(
                symbol="USDT",
                contract_address="0x55d398326f99059fF775485246999027B3197955",
                decimals=18,  # BSC USDT имеет 18 decimals
            ),
            "USDC": TokenConfig(
                symbol="USDC",
                contract_address="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
                decimals=18,  # BSC USDC имеет 18 decimals
                variant="bridged",
                bridge="binance-peg",
            ),
        },
    ),
    "polygon": ChainConfig(
        name="Polygon PoS",
        chain_id=137,
        rpc_url="https://polygon-rpc.com",
        confirmations=12,
        reorg_buffer=20,
        scan_window=400,  # Polygon RPC ограничивает до 500 блоков
        block_time_sec=2.1,
        native_symbol="MATIC",
        explorer_url="https://polygonscan.com",
        tokens={
            "USDT": TokenConfig(
                symbol="USDT",
                contract_address="0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
                decimals=6,
            ),
            "USDC": TokenConfig(
                symbol="USDC",
                contract_address="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
                decimals=6,
            ),
        },
    ),
    "avax": ChainConfig(
        name="Avalanche C-Chain",
        chain_id=43114,
        rpc_url="https://api.avax.network/ext/bc/C/rpc",
        confirmations=12,
        reorg_buffer=30,
        scan_window=2000,
        block_time_sec=2.0,
        native_symbol="AVAX",
        explorer_url="https://snowtrace.io",
        tokens={
            "USDT": TokenConfig(
                symbol="USDT",
                contract_address="0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
                decimals=6,
                variant="bridged",
                bridge="avalanche-bridge",
            ),
            "USDC": TokenConfig(
                symbol="USDC",
                contract_address="0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
                decimals=6,
            ),
        },
    ),
    "optimism": ChainConfig(
        name="Optimism",
        chain_id=10,
        rpc_url="https://mainnet.optimism.io",
        confirmations=12,
        reorg_buffer=20,
        scan_window=2000,
        block_time_sec=2.0,
        native_symbol="ETH",
        explorer_url="https://optimistic.etherscan.io",
        tokens={
            "USDT": TokenConfig(
                symbol="USDT",
                contract_address="0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
                decimals=6,
                variant="bridged",
                bridge="optimism-bridge",
            ),
            "USDC": TokenConfig(
                symbol="USDC",
                contract_address="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
                decimals=6,
            ),
        },
    ),
}


@lru_cache(maxsize=32)
def get_chain_config(chain: str) -> ChainConfig:
    """
    Получить конфигурацию сети по имени.

    Результат кэшируется для избежания повторных lookups.
    """
    normalized = {
        "arb": "arbitrum",
        "bnb": "bsc",
        "opt": "optimism",
    }.get(chain.lower(), chain.lower())
    config = CHAINS_CONFIG.get(normalized)
    if not config:
        raise ValueError(
            f"Unknown chain: {chain}. Supported: {list(CHAINS_CONFIG.keys())}"
        )
    return config


def get_all_chains() -> list[str]:
    """Получить список всех поддерживаемых сетей."""
    return list(CHAINS_CONFIG.keys())


def get_all_tokens() -> list[str]:
    """Получить список всех поддерживаемых токенов."""
    return ["USDT", "USDC"]


def get_token_contract(chain: str, token: str) -> str:
    """Получить адрес контракта токена для сети."""
    chain_config = get_chain_config(chain)
    token_config = chain_config.get_token(token)
    if not token_config:
        raise ValueError(f"Token {token} not supported on {chain}")
    return token_config.contract_address


def get_token_decimals(chain: str, token: str) -> int:
    """Получить количество decimals токена для сети."""
    chain_config = get_chain_config(chain)
    token_config = chain_config.get_token(token)
    if not token_config:
        raise ValueError(f"Token {token} not supported on {chain}")
    return token_config.decimals


def parse_token_amount(amount: int | str, chain: str, token: str) -> Decimal:
    """Преобразовать raw amount в human-readable Decimal."""
    decimals = get_token_decimals(chain, token)
    raw = int(amount) if isinstance(amount, str) else amount
    return Decimal(raw) / Decimal(10**decimals)


def to_raw_amount(amount: Decimal | str, chain: str, token: str) -> int:
    """Преобразовать human-readable amount в raw (wei-like)."""
    decimals = get_token_decimals(chain, token)
    dec_amount = Decimal(str(amount))
    return int(dec_amount * Decimal(10**decimals))


# ERC20 Transfer event signature
TRANSFER_EVENT_SIGNATURE = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# Minimal ERC20 ABI для transfer и balanceOf
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]
