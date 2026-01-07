"""
Конфигурация pytest для E2E тестов.
"""

import asyncio
import os
from typing import AsyncGenerator

import pytest
import pytest_asyncio
import httpx
from web3 import Web3
from eth_account import Account

from src.core.config import get_settings


@pytest.fixture(scope="session")
def event_loop():
    """Создать event loop для всей тестовой сессии."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def settings():
    """Получить настройки приложения."""
    return get_settings()


@pytest.fixture(scope="session")
def api_base_url() -> str:
    """Базовый URL API."""
    return os.getenv("API_BASE_URL", "http://localhost:8123")


@pytest.fixture(scope="session")
def merchant_api_key(settings) -> str:
    """API ключ мерчанта для тестов."""
    # Используем ключ из env или дефолтный для тестов
    key = os.getenv("TEST_MERCHANT_API_KEY", "0713d50377810b07229272820e0b57c2")
    return key


@pytest.fixture(scope="session")
def funder_private_key(settings) -> str:
    """Приватный ключ funder кошелька."""
    return settings.funder_private_key


@pytest.fixture(scope="session")
def treasury_address(settings) -> str:
    """Адрес treasury кошелька."""
    return settings.treasury_address


@pytest_asyncio.fixture
async def http_client(api_base_url: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP клиент для API запросов."""
    async with httpx.AsyncClient(
        base_url=api_base_url,
        timeout=30.0,
    ) as client:
        yield client


# ERC20 ABI для работы с токенами
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# Конфигурация сетей для тестов
CHAIN_CONFIGS = {
    "base": {
        "chain_id": 8453,
        "rpc_url": "https://1rpc.io/base",
        "native_symbol": "ETH",
        "tokens": {
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
    "arbitrum": {
        "chain_id": 42161,
        "rpc_url": "https://arb1.arbitrum.io/rpc",
        "native_symbol": "ETH",
        "tokens": {
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
    "bsc": {
        "chain_id": 56,
        "rpc_url": "https://bsc-dataseed1.binance.org/",
        "native_symbol": "BNB",
        "tokens": {
            "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "USDT": "0x55d398326f99059fF775485246999027B3197955",
        },
        "decimals": {"USDC": 18, "USDT": 18},
    },
    "polygon": {
        "chain_id": 137,
        "rpc_url": "https://polygon-rpc.com",
        "native_symbol": "MATIC",
        "tokens": {
            "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
    "avax": {
        "chain_id": 43114,
        "rpc_url": "https://api.avax.network/ext/bc/C/rpc",
        "native_symbol": "AVAX",
        "tokens": {
            "USDC": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
            "USDT": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
    "optimism": {
        "chain_id": 10,
        "rpc_url": "https://mainnet.optimism.io",
        "native_symbol": "ETH",
        "tokens": {
            "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
}
