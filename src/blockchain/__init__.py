"""
Blockchain модуль.
"""

from src.blockchain.chains import (
    ERC20_ABI,
    ChainConfig,
    ChainType,
    TokenConfig,
    get_aliases,
    get_all_chains,
    get_all_tokens,
    get_chain_config,
    get_evm_chains,
    get_gas_buffer_percent,
    get_max_gas_top_up_retries,
    get_multicall3_address,
    get_non_evm_chains,
    get_rpc_urls,
    get_token_contract,
    get_token_decimals,
    get_transfer_event_signature,
    is_chain_supported,
    is_evm_chain,
    normalize_chain_name,
    parse_token_amount,
    reload_config,
    to_raw_amount,
)
from src.blockchain.evm_adapter import EvmAdapter, get_evm_adapter

# Backward compatibility: TRANSFER_EVENT_SIGNATURE as a value
TRANSFER_EVENT_SIGNATURE = get_transfer_event_signature()

__all__ = [
    # Config
    "ChainConfig",
    "TokenConfig",
    "ChainType",
    "ERC20_ABI",
    "TRANSFER_EVENT_SIGNATURE",
    # Functions
    "get_chain_config",
    "get_all_chains",
    "get_evm_chains",
    "get_non_evm_chains",
    "get_all_tokens",
    "get_token_contract",
    "get_token_decimals",
    "get_transfer_event_signature",
    "get_multicall3_address",
    "get_rpc_urls",
    "get_rpc_env_var",
    "get_aliases",
    "get_gas_buffer_percent",
    "get_max_gas_top_up_retries",
    "parse_token_amount",
    "to_raw_amount",
    "is_chain_supported",
    "is_evm_chain",
    "normalize_chain_name",
    "reload_config",
    # Adapter
    "EvmAdapter",
    "get_evm_adapter",
]
