"""
Скрипт для проверки балансов всех депозитных адресов из БД.
"""

import asyncio
import sys
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_chain_config
from src.blockchain.evm_adapter import get_evm_adapter
from src.core.config import get_settings
from src.db.models import PaymentSession, DepositAddress, Invoice


async def check_balance(
    adapter, address: str, token_contract: str, token: str, chain: str
):
    """Проверить баланс токена на адресе."""
    try:
        balance = await adapter.get_erc20_balance(address, token_contract)
        native_balance = await adapter.get_native_balance_wei(address)
        return {
            "success": True,
            "balance": balance,
            "native_balance_wei": native_balance,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
        }


async def main():
    settings = get_settings()

    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    print("🔍 Проверяю балансы всех депозитных адресов...\n")

    async with SessionLocal() as session:
        # Получаем все payment sessions
        stmt = (
            select(PaymentSession)
            .options(
                selectinload(PaymentSession.deposit_address),
                selectinload(PaymentSession.invoice),
            )
            .join(Invoice)
        )

        result = await session.execute(stmt)
        sessions = result.scalars().all()

        print(f"📦 Найдено {len(sessions)} payment sessions\n")

        total_balances = {}
        addresses_with_balance = []

        for i, ps in enumerate(sessions, 1):
            deposit = ps.deposit_address
            chain = ps.chain
            token = ps.token

            try:
                adapter = get_evm_adapter(chain)
                chain_config = get_chain_config(chain)
                token_config = chain_config.tokens.get(token.upper())

                if not token_config:
                    print(
                        f"❌ [{i}/{len(sessions)}] {chain}/{token}: token not configured"
                    )
                    continue

                result = await check_balance(
                    adapter,
                    deposit.address,
                    token_config.contract_address,
                    token,
                    chain,
                )

                if not result["success"]:
                    print(
                        f"❌ [{i}/{len(sessions)}] {chain}/{token} {deposit.address[:16]}...: {result['error']}"
                    )
                    continue

                balance = result["balance"]
                native_balance = result["native_balance_wei"]

                # Суммируем балансы
                key = f"{chain.upper()}/{token}"
                if key not in total_balances:
                    total_balances[key] = Decimal("0")
                total_balances[key] += balance

                if balance > 0:
                    print(
                        f"💰 [{i}/{len(sessions)}] {chain.upper()}/{token}: {balance} (gas: {native_balance} wei)"
                    )
                    print(f"   📍 {deposit.address}")
                    addresses_with_balance.append(
                        {
                            "chain": chain,
                            "token": token,
                            "address": deposit.address,
                            "balance": balance,
                            "native_balance_wei": native_balance,
                        }
                    )
                else:
                    print(
                        f"⚪ [{i}/{len(sessions)}] {chain.upper()}/{token}: 0 (gas: {native_balance} wei) - {deposit.address[:16]}..."
                    )

            except Exception as e:
                print(
                    f"❌ [{i}/{len(sessions)}] {chain}/{token} {deposit.address[:16]}...: {e}"
                )

        print("\n" + "=" * 80)
        print("📊 ИТОГО:")
        print("=" * 80)

        if total_balances:
            for asset, total in sorted(total_balances.items()):
                if total > 0:
                    print(f"  {asset}: {total}")
        else:
            print("  Нет ненулевых балансов")

        print(f"\n💰 Адресов с балансом: {len(addresses_with_balance)}")

        if addresses_with_balance:
            print("\n" + "=" * 80)
            print("📋 ДЕТАЛИ АДРЕСОВ С БАЛАНСОМ:")
            print("=" * 80)
            for addr_info in addresses_with_balance:
                print(
                    f"\n{addr_info['chain'].upper()}/{addr_info['token']}: {addr_info['balance']}"
                )
                print(f"  Address: {addr_info['address']}")
                print(f"  Gas: {addr_info['native_balance_wei']} wei")


if __name__ == "__main__":
    asyncio.run(main())
