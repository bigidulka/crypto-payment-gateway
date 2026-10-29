"""Wallet payment requests: ERC-681, Solana Pay and Tonkeeper transfer URIs."""

from decimal import Decimal
from urllib.parse import urlencode

from src.blockchain.chains import ChainType, get_chain_config
from src.ledger.amounts import decimal_to_atomic


def checkout_uri(chain: str, token: str, amount: Decimal, address: str, reference: str) -> str:
    config = get_chain_config(chain)
    atomic = decimal_to_atomic(amount, config.get_asset_decimals(token))
    contract = "" if config.is_native_asset(token) else config.get_asset_contract(token)
    if config.chain_type == ChainType.SOLANA:
        query = {"amount": format(amount, "f"), "memo": reference}
        if contract:
            query["spl-token"] = contract
        return f"solana:{address}?" + urlencode(query)
    if config.chain_type == ChainType.TON:
        query = {"amount": str(atomic), "text": reference}
        if contract:
            query["jetton"] = contract
        return f"ton://transfer/{address}?" + urlencode(query)
    if contract:
        query = {"address": address, "uint256": str(atomic)}
        return f"ethereum:{contract}@{config.chain_id}/transfer?" + urlencode(query)
    return f"ethereum:{address}@{config.chain_id}?" + urlencode({"value": str(atomic)})
