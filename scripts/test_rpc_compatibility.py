#!/usr/bin/env python3
"""
🚀 RPC Full Compatibility Test

Проверяет ВСЕ методы, необходимые для работы:
- Persistent Poller (мониторинг депозитов)
- Sweeper (перевод токенов)

Методы:
1. eth_blockNumber - получение последнего блока
2. eth_getLogs (simple) - получение логов событий
3. eth_getLogs (OR topics) - batch запросы для нескольких адресов
4. eth_getBalance - баланс нативного токена
5. eth_call (balanceOf) - баланс ERC20 токена
6. eth_call (Multicall3) - batch запросы балансов
7. eth_gasPrice / eth_maxPriorityFeePerGas - параметры газа
8. eth_feeHistory - история fee (EIP-1559)
9. eth_estimateGas - оценка газа для транзакции
10. eth_getTransactionCount - получение nonce
11. eth_getTransactionReceipt - receipt транзакции
"""

import asyncio
import time
import sys
import os
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import AsyncWeb3, AsyncHTTPProvider

# Transfer event signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Multicall3 (одинаковый на всех EVM сетях)
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"


class MethodStatus(Enum):
    OK = "✅"
    FAIL = "❌"
    SKIP = "⚪"


@dataclass
class MethodResult:
    """Результат теста метода."""

    name: str
    status: MethodStatus
    latency_ms: float = 0.0
    error: Optional[str] = None


@dataclass
class RPCTestResult:
    """Полный результат тестирования RPC."""

    url: str
    chain: str
    is_alive: bool = False
    avg_latency_ms: float = 9999.0
    methods: dict = field(default_factory=dict)

    # Критичные для работы
    supports_block_number: bool = False
    supports_get_logs: bool = False
    supports_or_topics: bool = False
    supports_get_balance: bool = False
    supports_erc20_balance: bool = False
    supports_multicall: bool = False
    supports_gas_price: bool = False
    supports_fee_history: bool = False
    supports_estimate_gas: bool = False
    supports_nonce: bool = False
    supports_receipt: bool = False

    @property
    def critical_methods_ok(self) -> bool:
        """Все критичные методы работают."""
        return all(
            [
                self.supports_block_number,
                self.supports_get_logs,
                self.supports_get_balance,
                self.supports_erc20_balance,
                self.supports_gas_price,
                self.supports_nonce,
            ]
        )

    @property
    def score(self) -> float:
        """Вычислить скор RPC (меньше = лучше)."""
        if not self.is_alive:
            return 99999.0

        score = self.avg_latency_ms

        # Штрафы за отсутствие методов
        if not self.supports_or_topics:
            score += 3000  # OR topics очень важен
        if not self.supports_multicall:
            score += 1000  # Multicall ускоряет работу
        if not self.supports_fee_history:
            score += 500  # EIP-1559 опционален
        if not self.supports_estimate_gas:
            score += 200
        if not self.supports_receipt:
            score += 200

        return score

    @property
    def passed_count(self) -> int:
        """Количество пройденных тестов."""
        return sum(1 for m in self.methods.values() if m.status == MethodStatus.OK)

    @property
    def total_count(self) -> int:
        """Общее количество тестов."""
        return len(self.methods)


# Конфигурация сетей
CHAINS_CONFIG = {
    "arbitrum": {
        "usdt": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "chain_id": 42161,
    },
    "base": {
        "usdt": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "chain_id": 8453,
    },
    "bsc": {
        "usdt": "0x55d398326f99059fF775485246999027B3197955",
        "usdc": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "chain_id": 56,
    },
    "polygon": {
        "usdt": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "usdc": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "chain_id": 137,
    },
    "avax": {
        "usdt": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        "usdc": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        "chain_id": 43114,
    },
    "optimism": {
        "usdt": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        "usdc": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "chain_id": 10,
    },
}

# RPC списки
RPC_LISTS = {
    "arbitrum": [
        "https://arbitrum.gateway.tenderly.co",
        "https://arbitrum-one.public.blastapi.io",
        "https://arbitrum-one-public.nodies.app",
        "https://arb1.lava.build",
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.meowrpc.com",
        "https://arb-one-mainnet.gateway.tatum.io",
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arbitrum.drpc.org",
        "https://1rpc.io/arb",
        "https://arbitrum.public.blockpi.network/v1/rpc/public",
        "https://api.zan.top/arb-one",
    ],
    "base": [
        "https://base-mainnet.gateway.tatum.io",
        "https://base.drpc.org",
        "https://base-mainnet.public.blastapi.io",
        "https://base.lava.build",
        "https://base-public.nodies.app",
        "https://mainnet.base.org",
        "https://1rpc.io/base",
        "https://base.meowrpc.com",
        "https://base-rpc.publicnode.com",
        "https://developer-access-mainnet.base.org",
        "https://gateway.tenderly.co/public/base",
        "https://base.rpc.blxrbdn.com",
        "https://api.zan.top/base-mainnet",
        "https://base.public.blockpi.network/v1/rpc/public",
        "https://endpoints.omniatech.io/v1/base/mainnet/public",
        "https://base.llamarpc.com",
    ],
    "bsc": [
        "https://0.48.club",
        "https://binance-smart-chain-public.nodies.app",
        "https://bsc.drpc.org",
        "https://bsc-mainnet.public.blastapi.io",
        "https://bsc.blockrazor.xyz",
        "https://rpc-bsc.48.club",
        "https://bsc.meowrpc.com",
        "https://1rpc.io/bnb",
        "https://bsc-rpc.publicnode.com",
        "https://bnb.rpc.subquery.network/public",
        "https://api.zan.top/bsc-mainnet",
    ],
    "polygon": [
        "https://gateway.tenderly.co/public/polygon",
        "https://polygon-public.nodies.app",
        "https://polygon.drpc.org",
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon-rpc.com",
        "https://polygon.lava.build",
        "https://polygon.gateway.tenderly.co",
        "https://1rpc.io/matic",
        "https://rpc-mainnet.matic.quiknode.pro",
        "https://api.zan.top/polygon-mainnet",
    ],
    "avax": [
        "https://avalanche-mainnet.gateway.tenderly.co",
        "https://avalanche.drpc.org",
        "https://avalanche-public.nodies.app/ext/bc/C/rpc",
        "https://api.avax.network/ext/bc/C/rpc",
        "https://1rpc.io/avax/c",
        "https://avalanche-c-chain-rpc.publicnode.com",
        "https://avalanche.api.onfinality.io/public/ext/bc/C/rpc",
        "https://api.zan.top/avax-mainnet/ext/bc/C/rpc",
    ],
    "optimism": [
        "https://gateway.tenderly.co/public/optimism",
        "https://optimism.gateway.tenderly.co",
        "https://optimism-rpc.publicnode.com",
        "https://optimism.drpc.org",
        "https://optimism-public.nodies.app",
        "https://optimism.rpc.subquery.network/public",
        "https://optimism.api.onfinality.io/public",
        "https://1rpc.io/op",
        "https://api.zan.top/opt-mainnet",
        "https://optimism.public.blockpi.network/v1/rpc/public",
        "https://endpoints.omniatech.io/v1/op/mainnet/public",
    ],
}


async def test_method(name: str, coro) -> MethodResult:
    """Выполнить тест метода с измерением времени."""
    start = time.perf_counter()
    try:
        await coro
        latency = (time.perf_counter() - start) * 1000
        return MethodResult(name, MethodStatus.OK, latency)
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return MethodResult(name, MethodStatus.FAIL, latency, str(e)[:80])


async def full_rpc_test(
    chain: str, rpc_url: str, timeout: float = 15.0
) -> RPCTestResult:
    """
    Полный тест RPC со всеми методами.
    """
    result = RPCTestResult(url=rpc_url, chain=chain)
    config = CHAINS_CONFIG.get(chain, {})
    token_address = config.get("usdt", config.get("usdc"))

    try:
        w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))

        # 1. eth_blockNumber (КРИТИЧНО)
        method = await test_method("eth_blockNumber", w3.eth.block_number)
        result.methods["block_number"] = method
        if method.status == MethodStatus.OK:
            result.supports_block_number = True
            result.is_alive = True
        else:
            return result  # Если блок не получить - RPC мёртв

        block_number = await w3.eth.block_number

        # 2. eth_getLogs (simple) (КРИТИЧНО для Persistent Poller)
        try:
            logs = await w3.eth.get_logs(
                {
                    "fromBlock": block_number - 10,
                    "toBlock": block_number,
                    "address": w3.to_checksum_address(token_address),
                    "topics": [TRANSFER_TOPIC],
                }
            )
            result.methods["get_logs"] = MethodResult("eth_getLogs", MethodStatus.OK, 0)
            result.supports_get_logs = True
        except Exception as e:
            result.methods["get_logs"] = MethodResult(
                "eth_getLogs", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 3. eth_getLogs с OR topics (ВАЖНО для масштабирования)
        try:
            test_topics = [
                "0x0000000000000000000000000000000000000000000000000000000000000001",
                "0x0000000000000000000000000000000000000000000000000000000000000002",
                "0x0000000000000000000000000000000000000000000000000000000000000003",
            ]
            await w3.eth.get_logs(
                {
                    "fromBlock": block_number - 10,
                    "toBlock": block_number,
                    "address": w3.to_checksum_address(token_address),
                    "topics": [TRANSFER_TOPIC, None, test_topics],
                }
            )
            result.methods["or_topics"] = MethodResult(
                "eth_getLogs (OR)", MethodStatus.OK, 0
            )
            result.supports_or_topics = True
        except Exception as e:
            error = str(e).lower()
            if "invalid" in error or "unsupported" in error or "array" in error:
                result.methods["or_topics"] = MethodResult(
                    "eth_getLogs (OR)", MethodStatus.FAIL, 0, "OR topics not supported"
                )
            else:
                result.methods["or_topics"] = MethodResult(
                    "eth_getLogs (OR)", MethodStatus.FAIL, 0, str(e)[:60]
                )

        # 4. eth_getBalance (КРИТИЧНО для Sweeper)
        try:
            await w3.eth.get_balance(
                w3.to_checksum_address("0x0000000000000000000000000000000000000001")
            )
            result.methods["get_balance"] = MethodResult(
                "eth_getBalance", MethodStatus.OK, 0
            )
            result.supports_get_balance = True
        except Exception as e:
            result.methods["get_balance"] = MethodResult(
                "eth_getBalance", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 5. eth_call (balanceOf) (КРИТИЧНО для ERC20)
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_address),
                abi=[
                    {
                        "constant": True,
                        "inputs": [{"name": "_owner", "type": "address"}],
                        "name": "balanceOf",
                        "outputs": [{"name": "balance", "type": "uint256"}],
                        "type": "function",
                    }
                ],
            )
            await contract.functions.balanceOf(
                w3.to_checksum_address("0x0000000000000000000000000000000000000001")
            ).call()
            result.methods["erc20_balance"] = MethodResult(
                "eth_call (balanceOf)", MethodStatus.OK, 0
            )
            result.supports_erc20_balance = True
        except Exception as e:
            result.methods["erc20_balance"] = MethodResult(
                "eth_call (balanceOf)", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 6. Multicall3 (ВАЖНО для оптимизации)
        try:
            multicall_abi = [
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
                }
            ]
            multicall = w3.eth.contract(
                address=w3.to_checksum_address(MULTICALL3_ADDRESS), abi=multicall_abi
            )
            # Простой тест - пустой вызов
            await multicall.functions.aggregate([]).call()
            result.methods["multicall"] = MethodResult("Multicall3", MethodStatus.OK, 0)
            result.supports_multicall = True
        except Exception as e:
            result.methods["multicall"] = MethodResult(
                "Multicall3", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 7. eth_gasPrice (КРИТИЧНО для Sweeper)
        try:
            await w3.eth.gas_price
            result.methods["gas_price"] = MethodResult(
                "eth_gasPrice", MethodStatus.OK, 0
            )
            result.supports_gas_price = True
        except Exception as e:
            result.methods["gas_price"] = MethodResult(
                "eth_gasPrice", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 8. eth_feeHistory (EIP-1559)
        try:
            await w3.eth.fee_history(5, "latest", [25, 50, 75])
            result.methods["fee_history"] = MethodResult(
                "eth_feeHistory", MethodStatus.OK, 0
            )
            result.supports_fee_history = True
        except Exception as e:
            result.methods["fee_history"] = MethodResult(
                "eth_feeHistory", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 9. eth_estimateGas (ВАЖНО для Sweeper)
        try:
            await w3.eth.estimate_gas(
                {
                    "from": "0x0000000000000000000000000000000000000001",
                    "to": "0x0000000000000000000000000000000000000002",
                    "value": 0,
                }
            )
            result.methods["estimate_gas"] = MethodResult(
                "eth_estimateGas", MethodStatus.OK, 0
            )
            result.supports_estimate_gas = True
        except Exception as e:
            # Может быть ошибка "insufficient funds" - это нормально
            error = str(e).lower()
            if "insufficient" in error or "balance" in error:
                result.methods["estimate_gas"] = MethodResult(
                    "eth_estimateGas", MethodStatus.OK, 0
                )
                result.supports_estimate_gas = True
            else:
                result.methods["estimate_gas"] = MethodResult(
                    "eth_estimateGas", MethodStatus.FAIL, 0, str(e)[:60]
                )

        # 10. eth_getTransactionCount (КРИТИЧНО для nonce)
        try:
            await w3.eth.get_transaction_count(
                w3.to_checksum_address("0x0000000000000000000000000000000000000001")
            )
            result.methods["nonce"] = MethodResult(
                "eth_getTransactionCount", MethodStatus.OK, 0
            )
            result.supports_nonce = True
        except Exception as e:
            result.methods["nonce"] = MethodResult(
                "eth_getTransactionCount", MethodStatus.FAIL, 0, str(e)[:60]
            )

        # 11. eth_getTransactionReceipt (ВАЖНО для подтверждений)
        try:
            # Используем известный tx hash (genesis или пустой результат - OK)
            await w3.eth.get_transaction_receipt(
                "0x0000000000000000000000000000000000000000000000000000000000000000"
            )
            result.methods["receipt"] = MethodResult(
                "eth_getTransactionReceipt", MethodStatus.OK, 0
            )
            result.supports_receipt = True
        except Exception as e:
            error = str(e).lower()
            # "not found" - метод работает, просто tx не существует
            if "not found" in error or "null" in error or "none" in error:
                result.methods["receipt"] = MethodResult(
                    "eth_getTransactionReceipt", MethodStatus.OK, 0
                )
                result.supports_receipt = True
            else:
                result.methods["receipt"] = MethodResult(
                    "eth_getTransactionReceipt", MethodStatus.FAIL, 0, str(e)[:60]
                )

        # Вычисляем среднюю latency
        latencies = [m.latency_ms for m in result.methods.values() if m.latency_ms > 0]
        if latencies:
            result.avg_latency_ms = sum(latencies) / len(latencies)
        else:
            # Делаем отдельный тест latency
            start = time.perf_counter()
            await w3.eth.block_number
            result.avg_latency_ms = (time.perf_counter() - start) * 1000

    except asyncio.TimeoutError:
        result.methods["connection"] = MethodResult(
            "Connection", MethodStatus.FAIL, 0, "Timeout"
        )
    except Exception as e:
        result.methods["connection"] = MethodResult(
            "Connection", MethodStatus.FAIL, 0, str(e)[:60]
        )

    return result


async def benchmark_chain(
    chain: str, rpcs: list[str], concurrency: int = 5
) -> list[RPCTestResult]:
    """Тестировать все RPC для одной сети."""
    semaphore = asyncio.Semaphore(concurrency)

    async def test_with_limit(rpc_url: str) -> RPCTestResult:
        async with semaphore:
            return await full_rpc_test(chain, rpc_url)

    tasks = [test_with_limit(rpc) for rpc in rpcs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_results = []
    for r in results:
        if isinstance(r, RPCTestResult):
            valid_results.append(r)

    return valid_results


def print_detailed_results(chain: str, results: list[RPCTestResult]):
    """Вывести детальные результаты."""
    sorted_results = sorted(results, key=lambda r: r.score)

    print(f"\n{'='*90}")
    print(f"  {chain.upper()} - Детальный отчёт")
    print(f"{'='*90}")

    headers = ["#", "RPC", "Latency", "Tests", "OR", "Multi", "Fee", "Score"]
    print(
        f"{headers[0]:<3} {headers[1]:<42} {headers[2]:<8} {headers[3]:<7} {headers[4]:<4} {headers[5]:<5} {headers[6]:<4} {headers[7]:<8}"
    )
    print("-" * 90)

    for i, r in enumerate(sorted_results[:12], 1):
        if r.is_alive:
            url_short = r.url[:40] + ".." if len(r.url) > 42 else r.url
            latency = f"{r.avg_latency_ms:.0f}ms"
            tests = f"{r.passed_count}/{r.total_count}"
            or_topics = "✅" if r.supports_or_topics else "❌"
            multicall = "✅" if r.supports_multicall else "❌"
            fee = "✅" if r.supports_fee_history else "❌"
            score = f"{r.score:.0f}"

            # Цвет по критичным методам
            status = "✅" if r.critical_methods_ok else "⚠️"

            print(
                f"{i:<3} {url_short:<42} {latency:<8} {tests:<7} {or_topics:<4} {multicall:<5} {fee:<4} {score:<8}"
            )
        else:
            url_short = r.url[:40] + ".." if len(r.url) > 42 else r.url
            print(
                f"{i:<3} {url_short:<42} {'DEAD':<8} {'-':<7} {'-':<4} {'-':<5} {'-':<4} {'-':<8}"
            )


def select_best_rpcs(results: list[RPCTestResult]) -> dict:
    """Выбрать лучшие RPC."""
    # Фильтруем: должны работать критичные методы + OR topics
    excellent = [r for r in results if r.critical_methods_ok and r.supports_or_topics]
    good = [r for r in results if r.critical_methods_ok]

    # Приоритет: отличные > хорошие
    candidates = excellent if excellent else good
    candidates.sort(key=lambda r: r.score)

    if not candidates:
        return {"primary": None, "secondary": [], "issues": "No working RPCs!"}

    primary = candidates[0]
    secondary = candidates[1:4]  # Берём до 3 резервных

    issues = []
    if not primary.supports_or_topics:
        issues.append("Primary doesn't support OR topics")
    if not primary.supports_multicall:
        issues.append("Primary doesn't support Multicall3")

    return {
        "primary": primary.url,
        "primary_result": primary,
        "secondary": [r.url for r in secondary],
        "issues": "; ".join(issues) if issues else None,
    }


async def main():
    """Основная функция."""
    print("=" * 90)
    print("🚀 RPC FULL COMPATIBILITY TEST")
    print("=" * 90)
    print()
    print("Проверяем все методы, необходимые для Persistent Poller и Sweeper:")
    print("  • eth_blockNumber, eth_getLogs, eth_getLogs (OR topics)")
    print("  • eth_getBalance, eth_call (balanceOf), Multicall3")
    print("  • eth_gasPrice, eth_feeHistory, eth_estimateGas")
    print("  • eth_getTransactionCount, eth_getTransactionReceipt")
    print()
    print("Это займёт 2-3 минуты...")
    print()

    all_results = {}
    best_rpcs = {}

    for chain, rpcs in RPC_LISTS.items():
        print(f"🔄 Тестирую {chain.upper()} ({len(rpcs)} RPC)...", flush=True)

        results = await benchmark_chain(chain, rpcs)
        all_results[chain] = results

        print_detailed_results(chain, results)

        best = select_best_rpcs(results)
        best_rpcs[chain] = best

        if best["primary"]:
            pr = best.get("primary_result")
            print(f"\n  🏆 ЛУЧШИЙ: {best['primary']}")
            if pr:
                print(
                    f"     Latency: {pr.avg_latency_ms:.0f}ms | Tests: {pr.passed_count}/{pr.total_count} | OR: {'✅' if pr.supports_or_topics else '❌'}"
                )
            if best.get("issues"):
                print(f"     ⚠️  {best['issues']}")
            if best["secondary"]:
                print(f"     Резервные: {len(best['secondary'])} RPC")

    # Финальный отчёт
    print("\n")
    print("=" * 90)
    print("📊 ИТОГОВЫЙ ОТЧЁТ")
    print("=" * 90)

    print(f"\n{'Chain':<12} {'Primary RPC':<45} {'Lat':<6} {'OR':<4} {'Multi'}")
    print("-" * 90)

    for chain, best in best_rpcs.items():
        if best["primary"]:
            pr = best.get("primary_result")
            url_short = (
                best["primary"][:43] + ".."
                if len(best["primary"]) > 45
                else best["primary"]
            )
            latency = f"{pr.avg_latency_ms:.0f}ms" if pr else "-"
            or_topics = "✅" if pr and pr.supports_or_topics else "❌"
            multicall = "✅" if pr and pr.supports_multicall else "❌"
            print(
                f"{chain:<12} {url_short:<45} {latency:<6} {or_topics:<4} {multicall}"
            )
        else:
            print(f"{chain:<12} {'❌ НЕТ РАБОЧИХ RPC':<45}")

    # Генерируем .env
    print("\n")
    print("=" * 90)
    print("📝 КОНФИГУРАЦИЯ ДЛЯ .env")
    print("=" * 90)
    print()

    env_lines = []
    env_lines.append(
        "# === Blockchain RPC (auto-generated by full compatibility test) ==="
    )
    env_lines.append(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    env_lines.append(
        "# All RPCs verified for: Persistent Poller + Sweeper compatibility"
    )
    env_lines.append("")
    env_lines.append("# Primary RPC (fastest with full compatibility)")

    chain_to_env = {
        "arbitrum": "ARB_RPC_URL",
        "base": "BASE_RPC_URL",
        "bsc": "BSC_RPC_URL",
        "polygon": "POLYGON_RPC_URL",
        "avax": "AVAX_RPC_URL",
        "optimism": "OPTIMISM_RPC_URL",
    }

    chain_to_env_multi = {
        "arbitrum": "ARB_RPC_URLS",
        "base": "BASE_RPC_URLS",
        "bsc": "BSC_RPC_URLS",
        "polygon": "POLYGON_RPC_URLS",
        "avax": "AVAX_RPC_URLS",
        "optimism": "OPTIMISM_RPC_URLS",
    }

    for chain, env_var in chain_to_env.items():
        best = best_rpcs.get(chain, {})
        if best.get("primary"):
            env_lines.append(f"{env_var}={best['primary']}")
        else:
            env_lines.append(f"# {env_var}=  # No working RPC found!")

    env_lines.append("")
    env_lines.append("# Secondary RPC (comma-separated, for failover)")

    for chain, env_var in chain_to_env_multi.items():
        best = best_rpcs.get(chain, {})
        if best.get("secondary"):
            env_lines.append(f"{env_var}={','.join(best['secondary'])}")
        else:
            env_lines.append(f"# {env_var}=")

    env_content = "\n".join(env_lines)
    print(env_content)

    # Сохраняем
    output_file = "rpc_benchmark_result.env"
    with open(output_file, "w") as f:
        f.write(env_content)

    print()
    print(f"✅ Конфигурация сохранена в: {output_file}")

    # Предупреждения
    issues = [(c, b.get("issues")) for c, b in best_rpcs.items() if b.get("issues")]
    if issues:
        print()
        print("⚠️  ПРЕДУПРЕЖДЕНИЯ:")
        for chain, issue in issues:
            print(f"   • {chain}: {issue}")

    return best_rpcs


if __name__ == "__main__":
    asyncio.run(main())
