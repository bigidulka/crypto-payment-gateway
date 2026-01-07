#!/usr/bin/env python3
"""
🚀 RPC Benchmark Tool

Тестирует все RPC endpoints на:
1. Latency (пинг) - время ответа
2. OR Topics support - поддержка batch запросов
3. Reliability - стабильность соединения

Выбирает лучшие RPC для каждой сети и генерирует конфигурацию для .env
"""

import asyncio
import json
import time
import sys
import os
from dataclasses import dataclass, field
from typing import Optional

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web3 import AsyncWeb3, AsyncHTTPProvider

# USDT Transfer event signature
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@dataclass
class RPCTestResult:
    """Результат тестирования RPC."""

    url: str
    chain: str
    is_alive: bool = False
    latency_ms: float = 9999.0
    supports_or_topics: bool = False
    block_height: int = 0
    error: Optional[str] = None

    @property
    def score(self) -> float:
        """Вычислить скор RPC (меньше = лучше)."""
        if not self.is_alive:
            return 99999.0

        score = self.latency_ms

        # Бонус за OR topics (критически важно)
        if not self.supports_or_topics:
            score += 5000  # Большой штраф

        return score


# Конфигурация сетей с токенами для тестирования
CHAINS_CONFIG = {
    "arbitrum": {
        "usdt": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "chain_id": 42161,
    },
    "base": {
        "usdt": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC на Base
        "chain_id": 8453,
    },
    "bsc": {
        "usdt": "0x55d398326f99059fF775485246999027B3197955",
        "chain_id": 56,
    },
    "polygon": {
        "usdt": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "chain_id": 137,
    },
    "avax": {
        "usdt": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        "chain_id": 43114,
    },
    "optimism": {
        "usdt": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        "chain_id": 10,
    },
}


# RPC списки для каждой сети (извлечено из файла rpc)
RPC_LISTS = {
    "arbitrum": [
        "https://arb-one-mainnet.gateway.tatum.io",
        "https://arbitrum.meowrpc.com",
        "https://arbitrum.public.blockpi.network/v1/rpc/public",
        "https://arbitrum.rpc.subquery.network/public",
        "https://arb1.lava.build",
        "https://arbitrum.drpc.org",
        "https://api.zan.top/arb-one",
        "https://arbitrum-one-rpc.publicnode.com",
        "https://arbitrum-one-public.nodies.app",
        "https://arbitrum.gateway.tenderly.co",
        "https://public-arb-mainnet.fastnode.io",
        "https://arbitrum-one.public.blastapi.io",
        "https://arb1.arbitrum.io/rpc",
        "https://1rpc.io/arb",
    ],
    "base": [
        "https://base-mainnet.gateway.tatum.io",
        "https://base-public.nodies.app",
        "https://1rpc.io/base",
        "https://base-rpc.publicnode.com",
        "https://base.public.blockpi.network/v1/rpc/public",
        "https://base-mainnet.public.blastapi.io",
        "https://base.lava.build",
        "https://gateway.tenderly.co/public/base",
        "https://base.drpc.org",
        "https://base.rpc.subquery.network/public",
        "https://api.zan.top/base-mainnet",
        "https://base.rpc.blxrbdn.com",
        "https://mainnet.base.org",
        "https://developer-access-mainnet.base.org",
        "https://base.meowrpc.com",
        "https://endpoints.omniatech.io/v1/base/mainnet/public",
        "https://base.llamarpc.com",
    ],
    "bsc": [
        "https://bsc-mainnet.public.blastapi.io",
        "https://bsc-rpc.publicnode.com",
        "https://binance-smart-chain-public.nodies.app",
        "https://bsc.meowrpc.com",
        "https://endpoints.omniatech.io/v1/bsc/mainnet/public",
        "https://1rpc.io/bnb",
        "https://0.48.club",
        "https://bsc.drpc.org",
        "https://rpc-bsc.48.club",
        "https://api.zan.top/bsc-mainnet",
        "https://bnb.rpc.subquery.network/public",
        "https://bsc-dataseed1.ninicoin.io",
        "https://binance.llamarpc.com",
        "https://bsc.blockrazor.xyz",
    ],
    "polygon": [
        "https://polygon-rpc.com",
        "https://1rpc.io/matic",
        "https://polygon-public.nodies.app",
        "https://rpc-mainnet.matic.quiknode.pro",
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.lava.build",
        "https://polygon.drpc.org",
        "https://polygon.rpc.subquery.network/public",
        "https://endpoints.omniatech.io/v1/matic/mainnet/public",
        "https://api.zan.top/polygon-mainnet",
        "https://gateway.tenderly.co/public/polygon",
        "https://polygon.gateway.tenderly.co",
    ],
    "avax": [
        "https://avalanche-c-chain-rpc.publicnode.com",
        "https://1rpc.io/avax/c",
        "https://api.zan.top/avax-mainnet/ext/bc/C/rpc",
        "https://api.avax.network/ext/bc/C/rpc",
        "https://avalanche-public.nodies.app/ext/bc/C/rpc",
        "https://avalanche-mainnet.gateway.tenderly.co",
        "https://avalanche.api.onfinality.io/public/ext/bc/C/rpc",
        "https://avalanche.drpc.org",
        "https://spectrum-01.simplystaking.xyz/avalanche-mn-rpc/ext/bc/C/rpc",
        "https://endpoints.omniatech.io/v1/avax/mainnet/public",
    ],
    "optimism": [
        "https://1rpc.io/op",
        "https://optimism.gateway.tenderly.co",
        "https://optimism-public.nodies.app",
        "https://gateway.tenderly.co/public/optimism",
        "https://mainnet.optimism.io",
        "https://public-op-mainnet.fastnode.io",
        "https://optimism-rpc.publicnode.com",
        "https://optimism.drpc.org",
        "https://optimism.api.onfinality.io/public",
        "https://optimism.rpc.subquery.network/public",
        "https://api.zan.top/opt-mainnet",
        "https://optimism.public.blockpi.network/v1/rpc/public",
        "https://endpoints.omniatech.io/v1/op/mainnet/public",
    ],
}


async def test_rpc(chain: str, rpc_url: str, timeout: float = 10.0) -> RPCTestResult:
    """
    Полный тест RPC endpoint.

    Проверяет:
    1. Соединение и latency
    2. Поддержку OR topics
    """
    result = RPCTestResult(url=rpc_url, chain=chain)

    try:
        w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))

        # Тест 1: Базовое соединение + latency
        start = time.perf_counter()
        block_number = await w3.eth.block_number
        latency = (time.perf_counter() - start) * 1000

        result.is_alive = True
        result.latency_ms = round(latency, 1)
        result.block_height = block_number

        # Тест 2: OR Topics support
        config = CHAINS_CONFIG.get(chain, {})
        token_address = config.get("usdt")

        if token_address:
            try:
                # Тестовые адреса для OR topics
                test_topics = [
                    "0x0000000000000000000000000000000000000000000000000000000000000001",
                    "0x0000000000000000000000000000000000000000000000000000000000000002",
                    "0x0000000000000000000000000000000000000000000000000000000000000003",
                ]

                # Запрос с OR topics
                await w3.eth.get_logs(
                    {
                        "fromBlock": block_number - 10,
                        "toBlock": block_number,
                        "address": token_address,
                        "topics": [
                            TRANSFER_TOPIC,  # Transfer event
                            None,  # from (любой)
                            test_topics,  # to (OR - массив адресов)
                        ],
                    }
                )
                result.supports_or_topics = True
            except Exception as e:
                error_str = str(e).lower()
                if (
                    "invalid" in error_str
                    or "unsupported" in error_str
                    or "array" in error_str
                ):
                    result.supports_or_topics = False
                else:
                    # Другая ошибка (не связанная с OR topics)
                    result.supports_or_topics = False
                    result.error = f"OR test error: {str(e)[:50]}"

    except asyncio.TimeoutError:
        result.error = "Timeout"
    except Exception as e:
        result.error = str(e)[:100]

    return result


async def benchmark_chain(
    chain: str, rpcs: list[str], concurrency: int = 5
) -> list[RPCTestResult]:
    """Тестировать все RPC для одной сети с ограничением параллелизма."""

    semaphore = asyncio.Semaphore(concurrency)

    async def test_with_limit(rpc_url: str) -> RPCTestResult:
        async with semaphore:
            return await test_rpc(chain, rpc_url)

    tasks = [test_with_limit(rpc) for rpc in rpcs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Фильтруем исключения
    valid_results = []
    for r in results:
        if isinstance(r, RPCTestResult):
            valid_results.append(r)
        elif isinstance(r, Exception):
            print(f"  ⚠️ Exception: {r}")

    return valid_results


def print_results_table(chain: str, results: list[RPCTestResult]):
    """Вывести таблицу результатов для сети."""

    # Сортируем по скору (лучшие первые)
    sorted_results = sorted(results, key=lambda r: r.score)

    print(f"\n{'='*80}")
    print(f"  {chain.upper()}")
    print(f"{'='*80}")
    print(f"{'#':<3} {'Status':<8} {'Latency':<10} {'OR Topics':<12} {'RPC URL':<45}")
    print(f"{'-'*80}")

    for i, r in enumerate(sorted_results[:15], 1):  # Top 15
        if r.is_alive:
            status = "✅ OK"
            latency = f"{r.latency_ms:.0f}ms"
            or_topics = "✅ YES" if r.supports_or_topics else "❌ NO"
        else:
            status = "❌ FAIL"
            latency = "-"
            or_topics = "-"

        url_short = r.url[:43] + ".." if len(r.url) > 45 else r.url
        print(f"{i:<3} {status:<8} {latency:<10} {or_topics:<12} {url_short:<45}")

    if len(sorted_results) > 15:
        print(f"    ... и ещё {len(sorted_results) - 15} RPC")


def select_best_rpcs(
    results: list[RPCTestResult], max_primary: int = 1, max_secondary: int = 3
) -> dict:
    """
    Выбрать лучшие RPC для сети.

    Критерии:
    1. Должен поддерживать OR topics
    2. Минимальный latency

    Returns:
        {
            "primary": "https://...",
            "secondary": ["https://...", ...]
        }
    """
    # Фильтруем только живые с OR topics
    good_rpcs = [r for r in results if r.is_alive and r.supports_or_topics]

    # Если нет RPC с OR topics, берём просто живые
    if not good_rpcs:
        good_rpcs = [r for r in results if r.is_alive]

    # Сортируем по latency
    good_rpcs.sort(key=lambda r: r.latency_ms)

    primary = good_rpcs[0].url if good_rpcs else None
    secondary = [r.url for r in good_rpcs[1 : max_secondary + 1]]

    return {
        "primary": primary,
        "secondary": secondary,
        "best_latency": good_rpcs[0].latency_ms if good_rpcs else None,
        "or_topics": good_rpcs[0].supports_or_topics if good_rpcs else False,
    }


async def main():
    """Основная функция бенчмарка."""

    print("=" * 80)
    print("🚀 RPC BENCHMARK TOOL")
    print("=" * 80)
    print()
    print("Тестируем все RPC на:")
    print("  • Latency (время ответа)")
    print("  • OR Topics support (критично для масштабирования)")
    print()
    print("Это займёт 1-2 минуты...")
    print()

    all_results = {}
    best_rpcs = {}

    for chain, rpcs in RPC_LISTS.items():
        print(f"🔄 Тестирую {chain.upper()} ({len(rpcs)} RPC)...", flush=True)

        results = await benchmark_chain(chain, rpcs)
        all_results[chain] = results

        # Выводим таблицу
        print_results_table(chain, results)

        # Выбираем лучшие
        best = select_best_rpcs(results)
        best_rpcs[chain] = best

        if best["primary"]:
            print(f"\n  🏆 ЛУЧШИЙ: {best['primary']}")
            print(
                f"     Latency: {best['best_latency']:.0f}ms | OR Topics: {'✅' if best['or_topics'] else '❌'}"
            )
            if best["secondary"]:
                print(f"     Резервные: {len(best['secondary'])} RPC")

    # Финальный отчёт
    print("\n")
    print("=" * 80)
    print("📊 ИТОГОВЫЙ ОТЧЁТ")
    print("=" * 80)

    # Таблица лучших
    print(f"\n{'Chain':<12} {'Primary RPC':<45} {'Latency':<10} {'OR Topics'}")
    print("-" * 80)

    for chain, best in best_rpcs.items():
        if best["primary"]:
            url_short = (
                best["primary"][:43] + ".."
                if len(best["primary"]) > 45
                else best["primary"]
            )
            latency = f"{best['best_latency']:.0f}ms"
            or_topics = "✅" if best["or_topics"] else "❌"
            print(f"{chain:<12} {url_short:<45} {latency:<10} {or_topics}")
        else:
            print(f"{chain:<12} {'❌ НЕТ РАБОЧИХ RPC':<45}")

    # Генерируем .env конфигурацию
    print("\n")
    print("=" * 80)
    print("📝 КОНФИГУРАЦИЯ ДЛЯ .env")
    print("=" * 80)
    print()

    env_lines = []
    env_lines.append("# === Blockchain RPC (auto-generated by benchmark) ===")
    env_lines.append(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    env_lines.append("")
    env_lines.append("# Primary RPC (fastest with OR topics support)")

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

    # Сохраняем в файл
    output_file = "rpc_benchmark_result.env"
    with open(output_file, "w") as f:
        f.write(env_content)

    print()
    print(f"✅ Конфигурация сохранена в: {output_file}")
    print()
    print("Скопируйте нужные строки в ваш .env файл")

    # Предупреждения
    no_or_topics = [
        c for c, b in best_rpcs.items() if b.get("primary") and not b.get("or_topics")
    ]
    if no_or_topics:
        print()
        print("⚠️  ВНИМАНИЕ: Следующие сети не имеют RPC с OR topics:")
        for chain in no_or_topics:
            print(f"   • {chain}")
        print("   Для этих сетей будут использоваться параллельные запросы")

    return best_rpcs


if __name__ == "__main__":
    asyncio.run(main())
