"""
Конфигурация pytest для E2E тестов.
"""

import asyncio
import os

# Tests must not rely on a developer's dotenv file: Settings intentionally has
# no implicit env_file. Set inert process-local defaults before any module
# imports call get_settings(); individual tests may still override them.
os.environ.setdefault("SECRET_KEY", "test-runtime-secret-at-least-thirty-two-characters")
os.environ.setdefault("ENCRYPTION_KEY", "hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=")
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


_DESTRUCTIVE_RESET_TOKEN = "ALLOW_ISOLATED_TEST_DATABASE_RESET"
_RESET_TABLES = (
    "address_lease_events",
    "api_keys",
    "chain_checkpoints",
    "deposit_addresses",
    "deposits",
    "invoice_events",
    "invoices",
    "merchants",
    "onchain_txs",
    "outbox_webhooks",
    "payment_sessions",
    "rails",
    "unified_sweep_jobs",
    "user_balances",
    "user_wallets",
    "wallet_addresses",
    "webhooks",
)


def _require_safe_test_database(database_url: str) -> str:
    """Refuse destructive cleanup unless all disposable-DB guards are met."""
    if os.getenv("TEST_ALLOW_DESTRUCTIVE_RESET") != _DESTRUCTIVE_RESET_TOKEN:
        pytest.fail("set TEST_ALLOW_DESTRUCTIVE_RESET to the documented explicit token")

    url = make_url(database_url)
    if url.drivername != "postgresql+asyncpg":
        pytest.fail("TEST_DATABASE_URL must use postgresql+asyncpg")
    if url.host not in {"127.0.0.1", "::1"}:
        pytest.fail("TEST_DATABASE_URL host must be an explicit loopback IP")
    if not url.database or not url.database.startswith("test_"):
        pytest.fail("TEST_DATABASE_URL database name must start with test_")
    if url.database == "arbitron_payment":
        pytest.fail("refusing the production-named arbitron_payment database")

    expected_database = os.getenv("TEST_EXPECTED_DATABASE")
    if expected_database != url.database:
        pytest.fail("TEST_EXPECTED_DATABASE must exactly match TEST_DATABASE_URL database")
    return url.database


@pytest_asyncio.fixture
async def test_session(monkeypatch) -> AsyncGenerator[AsyncSession, None]:
    """Provide one real, guarded disposable PostgreSQL session per DB test.

    The command must pass an explicit loopback URL to a random `test_*`
    database, the matching `TEST_EXPECTED_DATABASE`, and the destructive-reset
    token. The fixture never reads ambient `DATABASE_URL` or `.env`; it never
    truncates `alembic_version` and only clears the explicit table allowlist.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL must point to an isolated PostgreSQL database")
    expected_database = _require_safe_test_database(database_url)

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
            if current_database != expected_database:
                pytest.fail("connected database does not match TEST_EXPECTED_DATABASE")

            result = await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
            actual_tables = set(result.scalars())
            missing_tables = set(_RESET_TABLES) - actual_tables
            if missing_tables:
                pytest.fail(
                    f"TEST_DATABASE_URL is missing migrated tables: {sorted(missing_tables)}"
                )
            quoted_tables = ", ".join(f'"{table_name}"' for table_name in _RESET_TABLES)
            await connection.execute(
                text(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE")
            )

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:

            @asynccontextmanager
            async def _session_context():
                yield session

            monkeypatch.setattr(
                "src.workers.persistent_poller.get_session_context", _session_context
            )
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


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
