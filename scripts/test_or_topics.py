#!/usr/bin/env python3
"""
Тест поддержки OR topics (массив в topics[2]) для всех RPC.

OR topics позволяют запрашивать логи для МНОЖЕСТВА адресов за 1 RPC вызов
вместо N отдельных вызовов.

Стандарт: EIP-1186 / eth_getLogs поддерживает OR через массив в topics
"""

import asyncio
import sys
from web3 import AsyncWeb3

# USDT Transfer event signature
TRANSFER_SIGNATURE = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# Тестовые RPC endpoints
RPC_ENDPOINTS = {
    "base": {
        "rpc": "https://mainnet.base.org",
        "usdt": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",  # или USDC
    },
    "arbitrum": {
        "rpc": "https://arb1.arbitrum.io/rpc",
        "usdt": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    },
    "bsc": {
        "rpc": "https://bsc.api.pocket.network",  # Pocket Network
        "usdt": "0x55d398326f99059fF775485246999027B3197955",
    },
    "polygon": {
        "rpc": "https://polygon-rpc.com",
        "usdt": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    },
    "avax": {
        "rpc": "https://api.avax.network/ext/bc/C/rpc",
        "usdt": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
    },
    "optimism": {
        "rpc": "https://optimism-rpc.publicnode.com",  # PublicNode
        "usdt": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
    },
}


async def test_or_topics(chain: str, config: dict) -> dict:
    """
    Тест OR topics для конкретной сети.

    Returns:
        dict с результатами теста
    """
    w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(config["rpc"]))

    result = {
        "chain": chain,
        "rpc": config["rpc"],
        "supports_or_topics": False,
        "error": None,
        "logs_found": 0,
    }

    try:
        # Получаем текущий блок
        latest_block = await w3.eth.block_number

        # Polygon имеет ограничение 500 блоков, BSC тоже rate limited
        # Используем маленькое окно для надёжности
        block_window = 10 if chain in ["polygon", "bsc"] else 50
        from_block = latest_block - block_window

        # Создаём тестовые адреса для OR query (меньше адресов для избежания rate limit)
        # Используем нулевые адреса - они точно не получат трансферов, но RPC должен принять запрос
        test_addresses = [
            "0x" + "0" * 24 + f"{i:040x}"[-40:]  # Генерируем разные адреса
            for i in range(3)  # Только 3 адреса для теста
        ]

        # Паддим адреса до 32 байт (формат topics)
        padded_addresses = [
            "0x" + addr[2:].lower().zfill(64) for addr in test_addresses
        ]

        # Тест 1: Простой getLogs (должен работать)
        simple_filter = {
            "fromBlock": from_block,
            "toBlock": latest_block,
            "address": w3.to_checksum_address(config["usdt"]),
            "topics": [TRANSFER_SIGNATURE],
        }

        simple_logs = await w3.eth.get_logs(simple_filter)
        print(f"  [OK] Simple getLogs: {len(simple_logs)} logs")

        # Тест 2: OR topics в topic[2] (массив адресов получателей)
        or_filter = {
            "fromBlock": from_block,
            "toBlock": latest_block,
            "address": w3.to_checksum_address(config["usdt"]),
            "topics": [
                TRANSFER_SIGNATURE,  # Transfer event
                None,  # from: any
                padded_addresses,  # to: OR(addr1, addr2, ...) ← ТЕСТ!
            ],
        }

        or_logs = await w3.eth.get_logs(or_filter)
        result["supports_or_topics"] = True
        result["logs_found"] = len(or_logs)
        print(f"  [OK] OR Topics: {len(or_logs)} logs ✅")

    except Exception as e:
        result["error"] = str(e)
        error_msg = str(e)[:100]
        print(f"  [FAIL] Error: {error_msg}")

        # Некоторые ошибки не означают отсутствие поддержки
        if "rate limit" in error_msg.lower():
            result["error"] = "Rate limited - try again"
        elif "timeout" in error_msg.lower():
            result["error"] = "Timeout - try again"

    return result


async def main():
    print("=" * 60)
    print("Testing OR Topics support for all chains")
    print("=" * 60)
    print()

    results = []

    for chain, config in RPC_ENDPOINTS.items():
        print(f"\n[{chain.upper()}] Testing {config['rpc']}")
        result = await test_or_topics(chain, config)
        results.append(result)
        await asyncio.sleep(0.5)  # Небольшая пауза между запросами

    # Итоговая таблица
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"{'Chain':<12} {'OR Topics':<12} {'Notes'}")
    print("-" * 60)

    all_supported = True
    for r in results:
        status = "✅ YES" if r["supports_or_topics"] else "❌ NO"
        notes = r["error"] if r["error"] else f"{r['logs_found']} logs"
        print(f"{r['chain']:<12} {status:<12} {notes}")
        if not r["supports_or_topics"]:
            all_supported = False

    print("-" * 60)

    if all_supported:
        print("\n🎉 ALL CHAINS SUPPORT OR TOPICS!")
        print("   You can use batch queries to reduce RPC calls by 1000x")
    else:
        print("\n⚠️  Some chains don't support OR topics")
        print("   Fallback to individual queries may be needed")

    return 0 if all_supported else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
