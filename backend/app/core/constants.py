from enum import Enum

class ChainID(int, Enum):
    BNB = 56
    BASE = 8453
    ARBITRUM = 42161

# ERC20 ABI (Minimal for transfer and balance)
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
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Token Addresses (Mainnet)
# В реальном проекте лучше вынести в конфиг или БД
TOKEN_ADDRESSES = {
    "BNB": {
        "USDT": "0x55d398326f99059fF775485246999027B3197955", # BSC-USD
    },
    "BASE": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", # Base USDC
    },
    "ARBITRUM": {
        "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", # Arbitrum USDT
    }
}

# Native symbols
NATIVE_SYMBOLS = {
    "BNB": "BNB",
    "BASE": "ETH",
    "ARBITRUM": "ETH"
}
