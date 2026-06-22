"""
Конфигурация блокчейн сетей и токенов.

Все настройки загружаются из config/chains.toml.
Чтобы добавить/удалить сеть - отредактируйте TOML файл.

Поддерживаемые типы сетей:
- EVM: Ethereum, Base, Arbitrum, BSC, Polygon, Avalanche, Optimism
- Solana: Solana Mainnet (закомментирован в TOML)
- TON: The Open Network (закомментирован в TOML)
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # fallback for older Python


class ChainType(str, Enum):
    """Тип блокчейн сети."""
    EVM = "evm"
    SOLANA = "solana"
    TON = "ton"


@dataclass(frozen=True)
class TokenConfig:
    """Конфигурация токена."""
    symbol: str
    contract_address: str
    decimals: int = 18
    variant: str = "native"
    bridge: str | None = None


@dataclass(frozen=True)
class ChainConfig:
    """Конфигурация блокчейн сети."""
    name: str
    chain_id: int
    chain_type: ChainType
    rpc_url: str  # Primary RPC URL
    rpc_urls: list[str]  # All RPC URLs for failover
    confirmations: int
    reorg_buffer: int
    scan_window: int
    block_time_sec: float
    native_symbol: str
    native_decimals: int
    explorer_url: str
    address_length: int
    tokens: dict[str, TokenConfig] = field(default_factory=dict)
    treasury_address: str = ""
    # Deposit scanner configuration
    scanner_provider: str = "rpc"
    oklink_chain: str = ""
    scanner_page_limit: int = 20
    scanner_max_pages_per_address: int = 5
    scanner_max_log_pages_per_tx: int = 20
    scanner_request_delay_ms: int = 200
    # Gas configuration
    is_l2: bool = False
    max_gas_cost_native: float = 0.002  # In native token
    gas_multiplier: float = 1.25
    min_funder_balance: float = 0.01  # Minimum funder balance to be healthy

    @property
    def max_gas_cost_wei(self) -> int:
        """Get max gas cost in wei."""
        return int(self.max_gas_cost_native * 10**self.native_decimals)

    def get_token(self, symbol: str) -> TokenConfig | None:
        """Получить конфигурацию токена по символу."""
        return self.tokens.get(symbol.upper())

    def is_native_asset(self, symbol: str) -> bool:
        """Проверить, является ли asset нативной монетой сети."""
        return symbol.upper() == self.native_symbol.upper()

    def supports_asset(self, symbol: str) -> bool:
        """Проверить поддержку asset как ERC20 token или native coin."""
        return self.is_native_asset(symbol) or self.get_token(symbol) is not None

    def get_asset_decimals(self, symbol: str) -> int:
        """Получить decimals для ERC20 token или native coin."""
        if self.is_native_asset(symbol):
            return self.native_decimals
        token = self.get_token(symbol)
        if token is None:
            raise ValueError(f"Asset {symbol} not supported on chain")
        return token.decimals

    def get_asset_contract(self, symbol: str) -> str:
        """Получить token contract или zero-address marker для native asset."""
        if self.is_native_asset(symbol):
            return NATIVE_TOKEN_CONTRACT
        token = self.get_token(symbol)
        if token is None:
            raise ValueError(f"Asset {symbol} not supported on chain")
        return token.contract_address

    def get_explorer_tx_url(self, tx_hash: str) -> str:
        """Получить URL транзакции в эксплорере."""
        if self.chain_type == ChainType.SOLANA:
            return f"{self.explorer_url}/tx/{tx_hash}"
        if self.chain_type == ChainType.TON:
            return f"{self.explorer_url}/transaction/{tx_hash}"
        return f"{self.explorer_url}/tx/{tx_hash}"

    def get_explorer_address_url(self, address: str) -> str:
        """Получить URL адреса в эксплорере."""
        if self.chain_type == ChainType.TON:
            return f"{self.explorer_url}/address/{address}"
        return f"{self.explorer_url}/address/{address}"

    @property
    def is_evm(self) -> bool:
        """Проверить, является ли сеть EVM-совместимой."""
        return self.chain_type == ChainType.EVM


NATIVE_TOKEN_CONTRACT = "0x0000000000000000000000000000000000000000"


# Global config storage
_CHAINS_CONFIG: dict[str, ChainConfig] = {}
_ALIASES: dict[str, str] = {}
_TRANSFER_EVENT_SIGNATURE: str = ""
_GAS_BUFFER_PERCENT: int = 10
_MAX_GAS_TOP_UP_RETRIES: int = 3
_MULTICALL3_ADDRESS: str = ""
_CONFIG_LOADED: bool = False


def _get_config_path() -> Path:
    """Получить путь к файлу конфигурации."""
    # Сначала проверяем переменную окружения
    config_path = os.getenv("CHAINS_CONFIG_PATH")
    if config_path:
        return Path(config_path)
    
    # Ищем относительно корня проекта
    # Поддерживаем запуск из разных директорий
    possible_paths = [
        Path("config/chains.toml"),
        Path("../config/chains.toml"),
        Path("/app/config/chains.toml"),  # Docker
        Path(__file__).parent.parent.parent / "config" / "chains.toml",
    ]
    
    for path in possible_paths:
        if path.exists():
            return path
    
    raise FileNotFoundError(
        "chains.toml not found. Set CHAINS_CONFIG_PATH or place in config/chains.toml"
    )


def _load_config() -> None:
    """Загрузить конфигурацию из TOML файла."""
    global _ALIASES, _CHAINS_CONFIG, _CONFIG_LOADED, _GAS_BUFFER_PERCENT
    global _MAX_GAS_TOP_UP_RETRIES, _MULTICALL3_ADDRESS, _TRANSFER_EVENT_SIGNATURE
    
    if _CONFIG_LOADED:
        return
    
    config_path = _get_config_path()
    
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    
    # Load constants
    constants = data.get("constants", {})
    _TRANSFER_EVENT_SIGNATURE = constants.get(
        "transfer_event_signature",
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    _MULTICALL3_ADDRESS = constants.get(
        "multicall3_address",
        "0xcA11bde05977b3631167028862bE2a173976CA11"
    )
    _GAS_BUFFER_PERCENT = constants.get("gas_buffer_percent", 10)
    _MAX_GAS_TOP_UP_RETRIES = constants.get("max_gas_top_up_retries", 3)
    
    # Load aliases
    _ALIASES = data.get("aliases", {})
    
    # Load chains
    chains_data = data.get("chains", {})
    
    for chain_name, chain_data in chains_data.items():
        # Parse tokens
        tokens_data = chain_data.get("tokens", {})
        tokens = {}
        for token_symbol, token_data in tokens_data.items():
            tokens[token_symbol] = TokenConfig(
                symbol=token_data.get("symbol", token_symbol),
                contract_address=token_data["contract_address"],
                decimals=token_data.get("decimals", 18),
                variant=token_data.get("variant", "native"),
                bridge=token_data.get("bridge"),
            )
        
        # Parse chain type
        chain_type_str = chain_data.get("chain_type", "evm")
        chain_type = ChainType(chain_type_str)
        
        # Get RPC URLs
        rpc_urls = chain_data.get("rpc_urls", [])
        primary_rpc = rpc_urls[0] if rpc_urls else chain_data.get("rpc_url", "")
        
        _CHAINS_CONFIG[chain_name] = ChainConfig(
            name=chain_data["name"],
            chain_id=chain_data.get("chain_id", 0),
            chain_type=chain_type,
            rpc_url=primary_rpc,
            rpc_urls=rpc_urls,
            confirmations=chain_data.get("confirmations", 12),
            reorg_buffer=chain_data.get("reorg_buffer", 20),
            scan_window=chain_data.get("scan_window", 2000),
            block_time_sec=chain_data.get("block_time_sec", 2.0),
            native_symbol=chain_data.get("native_symbol", "ETH"),
            native_decimals=chain_data.get("native_decimals", 18),
            explorer_url=chain_data.get("explorer_url", ""),
            address_length=chain_data.get("address_length", 42),
            tokens=tokens,
            scanner_provider=chain_data.get("scanner_provider", "rpc"),
            oklink_chain=chain_data.get("oklink_chain", chain_name),
            scanner_page_limit=chain_data.get("scanner_page_limit", 20),
            scanner_max_pages_per_address=chain_data.get(
                "scanner_max_pages_per_address", 5
            ),
            scanner_max_log_pages_per_tx=chain_data.get(
                "scanner_max_log_pages_per_tx", 20
            ),
            scanner_request_delay_ms=chain_data.get("scanner_request_delay_ms", 200),
            is_l2=chain_data.get("is_l2", False),
            max_gas_cost_native=chain_data.get("max_gas_cost_native", 0.002),
            gas_multiplier=chain_data.get("gas_multiplier", 1.25),
            min_funder_balance=chain_data.get("min_funder_balance", 0.01),
        )
    
    _CONFIG_LOADED = True


def reload_config() -> None:
    """Перезагрузить конфигурацию (для hot-reload)."""
    global _CONFIG_LOADED
    _CONFIG_LOADED = False
    get_chain_config.cache_clear()
    _load_config()


def get_gas_buffer_percent() -> int:
    """Get gas buffer percent from config."""
    _load_config()
    return _GAS_BUFFER_PERCENT


def get_max_gas_top_up_retries() -> int:
    """Get max gas top up retries from config."""
    _load_config()
    return _MAX_GAS_TOP_UP_RETRIES


def get_transfer_event_signature() -> str:
    """Получить сигнатуру события Transfer."""
    _load_config()
    return _TRANSFER_EVENT_SIGNATURE


def get_multicall3_address() -> str:
    """Получить адрес контракта Multicall3."""
    _load_config()
    return _MULTICALL3_ADDRESS


def get_aliases() -> dict[str, str]:
    """Get chain aliases (short name -> canonical name)."""
    _load_config()
    return _ALIASES.copy()


def normalize_chain_name(chain: str) -> str:
    """
    Normalize chain name using aliases.
    
    Args:
        chain: Chain name or alias ('arb', 'opt', 'bnb')
    
    Returns:
        Canonical chain name ('arbitrum', 'optimism', 'bsc')
    """
    _load_config()
    chain_lower = chain.lower()
    return _ALIASES.get(chain_lower, chain_lower)


@lru_cache(maxsize=32)
def get_chain_config(chain: str) -> ChainConfig:
    """
    Получить конфигурацию сети по имени.
    Результат кэшируется для избежания повторных lookups.
    """
    _load_config()
    
    # Normalize chain name
    chain_lower = chain.lower()
    normalized = _ALIASES.get(chain_lower, chain_lower)
    
    config = _CHAINS_CONFIG.get(normalized)
    if not config:
        raise ValueError(
            f"Unknown chain: {chain}. Supported: {list(_CHAINS_CONFIG.keys())}"
        )
    return config


def get_all_chains() -> list[str]:
    """Получить список всех поддерживаемых сетей."""
    _load_config()
    return list(_CHAINS_CONFIG.keys())


def get_evm_chains() -> list[str]:
    """Получить список всех EVM сетей."""
    _load_config()
    return [name for name, cfg in _CHAINS_CONFIG.items() if cfg.chain_type == ChainType.EVM]


def get_non_evm_chains() -> list[str]:
    """Получить список всех non-EVM сетей."""
    _load_config()
    return [name for name, cfg in _CHAINS_CONFIG.items() if cfg.chain_type != ChainType.EVM]


def is_chain_supported(chain: str) -> bool:
    """Проверить, поддерживается ли сеть."""
    _load_config()
    chain_lower = chain.lower()
    normalized = _ALIASES.get(chain_lower, chain_lower)
    return normalized in _CHAINS_CONFIG


def is_evm_chain(chain: str) -> bool:
    """Проверить, является ли сеть EVM."""
    try:
        config = get_chain_config(chain)
        return config.chain_type == ChainType.EVM
    except ValueError:
        return False


def get_all_tokens() -> list[str]:
    """Получить список всех поддерживаемых ERC20/native assets."""
    _load_config()
    tokens = set()
    for chain_config in _CHAINS_CONFIG.values():
        tokens.update(chain_config.tokens.keys())
        tokens.add(chain_config.native_symbol.upper())
    return sorted(tokens)


def get_token_contract(chain: str, token: str) -> str:
    """Получить адрес контракта токена или native zero-address marker."""
    chain_config = get_chain_config(chain)
    return chain_config.get_asset_contract(token)


def get_token_decimals(chain: str, token: str) -> int:
    """Получить количество decimals ERC20 token или native asset."""
    chain_config = get_chain_config(chain)
    return chain_config.get_asset_decimals(token)


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


def get_rpc_urls(chain: str) -> list[str]:
    """Получить список RPC URLs для сети."""
    config = get_chain_config(chain)
    return config.rpc_urls


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
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]
