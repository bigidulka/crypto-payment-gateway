#!/usr/bin/env python
"""
Параллельный E2E тест всех сетей.

Запускает тесты на всех сетях одновременно, используя доступные балансы.
"""

import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from eth_account import Account
from web3 import Web3

from src.core.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Сумма для теста
TEST_AMOUNT = Decimal("0.01")

# Таймауты
MAX_WAIT_CONFIRMED = 300  # 5 минут
MAX_WAIT_SWEPT = 300  # 5 минут
POLL_INTERVAL = 3

# ERC20 ABI
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
]

# Конфигурация сетей
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
        "rpc_url": "https://1rpc.io/avax/c",
        "native_symbol": "AVAX",
        "tokens": {
            "USDC": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
            "USDT": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
    "optimism": {
        "chain_id": 10,
        "rpc_url": "https://1rpc.io/op",
        "native_symbol": "ETH",
        "tokens": {
            "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        },
        "decimals": {"USDC": 6, "USDT": 6},
    },
}


@dataclass
class TestResult:
    chain: str
    token: str
    success: bool
    status: str
    deposit_address: str = ""
    payment_tx: str = ""
    final_balance: Decimal = Decimal("0")
    duration: float = 0
    error: str = ""


async def check_balances(funder_address: str) -> dict[str, dict[str, Decimal]]:
    """Проверить балансы на всех сетях."""
    balances = {}

    for chain, config in CHAIN_CONFIGS.items():
        balances[chain] = {"native": Decimal("0")}

        try:
            w3 = Web3(
                Web3.HTTPProvider(config["rpc_url"], request_kwargs={"timeout": 15})
            )

            # Нативный баланс
            native = w3.eth.get_balance(funder_address)
            balances[chain]["native"] = Decimal(native) / Decimal(10**18)

            # Токены
            for token, addr in config["tokens"].items():
                try:
                    contract = w3.eth.contract(
                        address=Web3.to_checksum_address(addr),
                        abi=ERC20_ABI,
                    )
                    raw = contract.functions.balanceOf(
                        Web3.to_checksum_address(funder_address)
                    ).call()
                    decimals = config["decimals"][token]
                    balances[chain][token] = Decimal(raw) / Decimal(10**decimals)
                except Exception:
                    balances[chain][token] = Decimal("0")

        except Exception as e:
            logger.warning(f"[{chain}] Ошибка проверки баланса: {e}")
            for token in config["tokens"]:
                balances[chain][token] = Decimal("0")

    return balances


def select_tests(balances: dict, min_amount: Decimal) -> list[tuple[str, str]]:
    """Выбрать сети/токены для тестирования на основе балансов."""
    tests = []

    for chain, chain_balances in balances.items():
        # Сначала пробуем USDC, потом USDT
        for token in ["USDC", "USDT"]:
            if chain_balances.get(token, Decimal("0")) >= min_amount:
                tests.append((chain, token))
                break  # Только один токен на сеть

    return tests


async def run_single_test(
    chain: str,
    token: str,
    api_base_url: str,
    merchant_api_key: str,
    funder_private_key: str,
) -> TestResult:
    """Выполнить тест для одной сети."""
    result = TestResult(chain=chain, token=token, success=False, status="")
    start_time = time.time()

    config = CHAIN_CONFIGS[chain]

    try:
        w3 = Web3(Web3.HTTPProvider(config["rpc_url"], request_kwargs={"timeout": 60}))
        funder = Account.from_key(funder_private_key)
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(config["tokens"][token]),
            abi=ERC20_ABI,
        )
        decimals = config["decimals"][token]

        async with httpx.AsyncClient(base_url=api_base_url, timeout=30.0) as client:
            # 1. Создаём инвойс
            logger.info(f"[{chain}] Создаём инвойс на {TEST_AMOUNT} {token}")
            resp = await client.post(
                "/v1/invoices",
                json={
                    "amount": str(TEST_AMOUNT),
                    "asset": token,
                    "allowed_chains": [chain],
                    "ttl_minutes": 30,
                },
                headers={
                    "Authorization": f"Bearer {merchant_api_key}",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            )
            resp.raise_for_status()
            invoice = resp.json()
            public_id = invoice["public_id"]
            logger.info(f"[{chain}] Инвойс: {public_id}")

            # 2. Выбираем сеть
            resp = await client.post(
                f"/pay/{public_id}/select", json={"chain": chain, "token": token}
            )
            resp.raise_for_status()
            payment_info = resp.json()
            deposit_address = payment_info["deposit_address"]
            result.deposit_address = deposit_address
            logger.info(f"[{chain}] Депозит: {deposit_address}")

            # 3. Отправляем токены
            raw_amount = int(TEST_AMOUNT * (10**decimals))

            nonce = w3.eth.get_transaction_count(funder.address)
            gas_price = w3.eth.gas_price

            tx_data = token_contract.functions.transfer(
                Web3.to_checksum_address(deposit_address), raw_amount
            )
            gas_estimate = tx_data.estimate_gas({"from": funder.address})

            tx = tx_data.build_transaction(
                {
                    "from": funder.address,
                    "nonce": nonce,
                    "gas": int(gas_estimate * 1.3),
                    "gasPrice": int(gas_price * 1.5),
                    "chainId": config["chain_id"],
                }
            )

            signed = w3.eth.account.sign_transaction(tx, funder_private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            result.payment_tx = tx_hash.hex()

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] != 1:
                raise ValueError("Payment TX failed")

            logger.info(f"[{chain}] Платёж: {tx_hash.hex()[:16]}...")

            # 4. Ждём подтверждения
            confirmed = False
            for _ in range(MAX_WAIT_CONFIRMED // POLL_INTERVAL):
                resp = await client.get(f"/pay/{public_id}/status")
                status = resp.json()["status"].lower()

                if status == "confirmed":
                    confirmed = True
                    logger.info(f"[{chain}] ✅ Подтверждён!")
                    break
                elif status in ("expired", "failed"):
                    raise ValueError(f"Статус: {status}")

                await asyncio.sleep(POLL_INTERVAL)

            if not confirmed:
                result.status = "TIMEOUT_CONFIRM"
                result.error = "Таймаут подтверждения"
                return result

            # 5. Ждём sweep
            swept = False
            for _ in range(MAX_WAIT_SWEPT // POLL_INTERVAL):
                raw_balance = token_contract.functions.balanceOf(
                    Web3.to_checksum_address(deposit_address)
                ).call()

                if raw_balance == 0:
                    swept = True
                    logger.info(f"[{chain}] ✅ Sweep выполнен!")
                    break

                await asyncio.sleep(POLL_INTERVAL)

            if not swept:
                result.status = "TIMEOUT_SWEEP"
                result.error = "Таймаут sweep"
                result.final_balance = Decimal(raw_balance) / Decimal(10**decimals)
                return result

            result.success = True
            result.status = "SUCCESS"
            result.final_balance = Decimal("0")

    except Exception as e:
        result.error = str(e)[:100]
        result.status = "ERROR"
        logger.error(f"[{chain}] ❌ {e}")

    result.duration = time.time() - start_time
    return result


async def main():
    settings = get_settings()
    funder = Account.from_key(settings.funder_private_key)

    print("=" * 70)
    print("ПРОВЕРКА БАЛАНСОВ FUNDER")
    print("=" * 70)
    print(f"Адрес: {funder.address}")
    print()

    balances = await check_balances(funder.address)

    for chain, chain_bal in balances.items():
        native_sym = CHAIN_CONFIGS[chain]["native_symbol"]
        print(f"{chain.upper():12} {native_sym}: {chain_bal['native']:.6f}", end="")
        for token in ["USDC", "USDT"]:
            bal = chain_bal.get(token, Decimal("0"))
            status = "✅" if bal >= TEST_AMOUNT else "❌"
            print(f"  {token}: {bal:.2f} {status}", end="")
        print()

    print()

    # Выбираем тесты
    tests = select_tests(balances, TEST_AMOUNT)

    if not tests:
        print("❌ Нет достаточных балансов для тестов!")
        return

    print(f"Будут запущены тесты на {len(tests)} сетях:")
    for chain, token in tests:
        print(f"  - {chain}: {token}")
    print()

    input("Нажмите Enter для запуска тестов...")

    print()
    print("=" * 70)
    print("ЗАПУСК ПАРАЛЛЕЛЬНЫХ ТЕСТОВ")
    print("=" * 70)

    # Запускаем все тесты параллельно
    tasks = [
        run_single_test(
            chain=chain,
            token=token,
            api_base_url="http://localhost:8123",
            merchant_api_key="0713d50377810b07229272820e0b57c2",
            funder_private_key=settings.funder_private_key,
        )
        for chain, token in tests
    ]

    results = await asyncio.gather(*tasks)

    # Выводим результаты
    print()
    print("=" * 70)
    print("РЕЗУЛЬТАТЫ ТЕСТОВ")
    print("=" * 70)
    print(f"{'Сеть':<12} {'Токен':<6} {'Статус':<18} {'Время':<8} {'Ошибка'}")
    print("-" * 70)

    passed = 0
    failed = 0

    for r in results:
        status_icon = "✅" if r.success else "❌"
        time_str = f"{r.duration:.1f}s"
        error_str = r.error[:25] + "..." if len(r.error) > 25 else r.error
        print(
            f"{r.chain:<12} {r.token:<6} {status_icon} {r.status:<15} {time_str:<8} {error_str}"
        )

        if r.success:
            passed += 1
        else:
            failed += 1

    print("-" * 70)
    print(f"Пройдено: {passed}/{len(results)}, Провалено: {failed}/{len(results)}")
    print("=" * 70)

    # Анализ проблем
    if failed > 0:
        print()
        print("АНАЛИЗ ПРОБЛЕМНЫХ СЕТЕЙ:")
        print("-" * 70)

        for r in results:
            if not r.success:
                print(f"\n🔴 {r.chain.upper()} / {r.token}:")
                print(f"   Статус: {r.status}")
                print(f"   Ошибка: {r.error}")
                print(f"   Депозит: {r.deposit_address}")
                print(f"   TX: {r.payment_tx}")

                if r.status == "TIMEOUT_CONFIRM":
                    print(
                        "   💡 Решение: Проверить poller worker, возможно не сканирует сеть"
                    )
                elif r.status == "TIMEOUT_SWEEP":
                    print(
                        f"   💡 Решение: Проверить sweeper, остаток: {r.final_balance}"
                    )
                elif "RPC" in r.error or "timeout" in r.error.lower():
                    print("   💡 Решение: Сменить RPC провайдер")
                elif "insufficient" in r.error.lower():
                    print("   💡 Решение: Пополнить баланс газа на funder")


if __name__ == "__main__":
    asyncio.run(main())
