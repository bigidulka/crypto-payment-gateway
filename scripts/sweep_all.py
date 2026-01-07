"""
Скрипт для вывода средств со всех депозитных адресов.
Создаёт SweepJob для каждого адреса с балансом > 0.
"""

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_chain_config, get_token_contract
from src.blockchain.evm_adapter import get_evm_adapter
from src.core.config import get_settings
from src.db.models import (
    PaymentSession,
    DepositAddress,
    Invoice,
    SweepJob,
    SweepState,
)


async def main():
    settings = get_settings()

    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    print("🔍 Сканирую депозитные адреса...")

    async with SessionLocal() as session:
        # Получаем все payment sessions
        stmt = (
            select(PaymentSession)
            .options(
                selectinload(PaymentSession.deposit_address),
                selectinload(PaymentSession.invoice),
            )
            .join(Invoice)
            .where(
                Invoice.merchant_id == uuid.UUID("21704f22-cd4c-419a-b160-ecd5972edf68")
            )
        )

        result = await session.execute(stmt)
        sessions = result.scalars().all()

        print(f"📦 Найдено {len(sessions)} payment sessions\n")

        sweep_count = 0

        for ps in sessions:
            deposit = ps.deposit_address
            chain = ps.chain
            token = ps.token

            try:
                # Проверяем существующий sweep job
                existing_stmt = select(SweepJob).where(
                    SweepJob.payment_session_id == ps.id
                )
                existing_result = await session.execute(existing_stmt)
                existing_job = existing_result.scalar_one_or_none()

                # Получаем баланс
                adapter = get_evm_adapter(chain)
                chain_config = get_chain_config(chain)
                token_config = chain_config.tokens.get(token.upper())

                if not token_config:
                    print(f"⚠️  {chain}/{token}: token not configured")
                    continue

                balance = await adapter.get_erc20_balance(
                    deposit.address, token_config.contract_address
                )

                if balance <= 0:
                    continue

                print(
                    f"💰 {chain.upper()}/{token}: {balance} на {deposit.address[:16]}..."
                )

                # Если есть активный job - пропускаем
                if existing_job and existing_job.state in [
                    SweepState.PENDING_GAS,
                    SweepState.FUNDING,
                    SweepState.SWEEPING,
                ]:
                    print(f"   ⏳ Sweep job уже существует: {existing_job.state.value}")
                    continue

                # Создаём или перезапускаем sweep job
                if existing_job:
                    # Сбрасываем failed/completed job
                    existing_job.state = SweepState.PENDING_GAS
                    existing_job.attempts = 0
                    existing_job.last_error = None
                    existing_job.gas_tx_hash = None
                    existing_job.sweep_tx_hash = None
                    existing_job.next_retry_at = None
                    print(f"   🔄 Перезапускаю sweep job: {existing_job.id}")
                else:
                    # Создаём новый
                    new_job = SweepJob(
                        payment_session_id=ps.id,
                        state=SweepState.PENDING_GAS,
                        attempts=0,
                        max_attempts=5,
                    )
                    session.add(new_job)
                    print(f"   ✅ Создан sweep job")

                sweep_count += 1

            except Exception as e:
                print(f"   ❌ Ошибка: {e}")

        await session.commit()

    print(f"\n✅ Создано/перезапущено {sweep_count} sweep jobs")
    print("🚀 Sweeper worker начнёт обработку автоматически")


if __name__ == "__main__":
    asyncio.run(main())
