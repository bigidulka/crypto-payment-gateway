#!/usr/bin/env python
"""
Скрипт проверки балансов funder кошелька перед E2E тестами.

Проверяет:
1. Баланс нативных токенов (ETH/BNB/MATIC/AVAX) для оплаты газа
2. Баланс USDC/USDT на каждой сети

Запуск:
    python tests/check_funder_balances.py
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

from web3 import Web3
from eth_account import Account

from src.core.config import get_settings
from tests.conftest import CHAIN_CONFIGS, ERC20_ABI


# Минимальные требуемые балансы
MIN_NATIVE_BALANCE = {
    "base": Decimal("0.001"),  # ETH
    "arbitrum": Decimal("0.001"),  # ETH
    "bsc": Decimal("0.005"),  # BNB
    "polygon": Decimal("1.0"),  # MATIC
    "avax": Decimal("0.1"),  # AVAX
    "optimism": Decimal("0.001"),  # ETH
}

MIN_TOKEN_BALANCE = Decimal("1.0")  # Минимум 1 USDC/USDT для тестов


def check_balances():
    """Проверить балансы на всех сетях."""
    settings = get_settings()
    funder_account = Account.from_key(settings.funder_private_key)

    print("=" * 70)
    print(f"FUNDER WALLET: {funder_account.address}")
    print("=" * 70)
    print()

    all_ok = True

    for chain, config in CHAIN_CONFIGS.items():
        print(f"\n{'─' * 50}")
        print(f"🔗 {chain.upper()}")
        print(f"{'─' * 50}")

        try:
            w3 = Web3(
                Web3.HTTPProvider(config["rpc_url"], request_kwargs={"timeout": 30})
            )

            if not w3.is_connected():
                print(f"  ❌ Не удалось подключиться к RPC")
                all_ok = False
                continue

            # Баланс нативного токена
            native_balance = w3.eth.get_balance(funder_account.address)
            native_decimal = Decimal(native_balance) / Decimal(10**18)
            min_native = MIN_NATIVE_BALANCE.get(chain, Decimal("0.001"))
            native_ok = native_decimal >= min_native

            status = "✅" if native_ok else "⚠️"
            print(
                f"  {status} {config['native_symbol']}: {native_decimal:.6f} (min: {min_native})"
            )

            if not native_ok:
                all_ok = False

            # Балансы токенов
            for token, contract_addr in config["tokens"].items():
                try:
                    contract = w3.eth.contract(
                        address=Web3.to_checksum_address(contract_addr),
                        abi=ERC20_ABI,
                    )

                    decimals = config["decimals"][token]
                    raw_balance = contract.functions.balanceOf(
                        funder_account.address
                    ).call()
                    token_balance = Decimal(raw_balance) / Decimal(10**decimals)

                    token_ok = token_balance >= MIN_TOKEN_BALANCE
                    status = "✅" if token_ok else "⚠️"
                    print(
                        f"  {status} {token}: {token_balance:.2f} (min: {MIN_TOKEN_BALANCE})"
                    )

                    if not token_ok:
                        all_ok = False

                except Exception as e:
                    print(f"  ❌ {token}: Ошибка - {e}")
                    all_ok = False

        except Exception as e:
            print(f"  ❌ Ошибка подключения: {e}")
            all_ok = False

    print()
    print("=" * 70)
    if all_ok:
        print("✅ Все балансы достаточны для запуска тестов")
    else:
        print("⚠️  Некоторые балансы недостаточны!")
        print("   Пополните кошелёк перед запуском тестов")
    print("=" * 70)

    return all_ok


if __name__ == "__main__":
    success = check_balances()
    sys.exit(0 if success else 1)
