"""
Sweeper Worker.
Переводит средства с deposit-адресов на treasury.

Pipeline статусов:
1. pending_gas  - Ожидание/отправка газа (для токенов ERC-20)
2. funding      - Газ отправлен, ждём подтверждения
3. sweeping     - Sweep транзакция отправлена
4. completed    - Готово
5. failed       - Ошибка
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.blockchain.chains import (
    CHAINS_CONFIG,
    get_chain_config,
    get_token_contract,
    to_raw_amount,
)
from src.blockchain.evm_adapter import EvmAdapter, get_evm_adapter
from src.core.config import get_settings
from src.crypto.encryption import decrypt_private_key
from src.crypto.hd_wallet import HDWallet
from src.db.models import DepositAddress, SweepJob, SweepState, PaymentSession, Invoice
from src.db.redis import redis_lock
from src.db.session import get_session_context

logger = logging.getLogger(__name__)


@dataclass
class GasEstimate:
    """Результат оценки газа для sweep."""

    gas_units: int
    gas_price: int  # wei per gas unit
    total_cost_wei: int
    max_fee_per_gas: int | None = None  # EIP-1559
    max_priority_fee: int | None = None  # EIP-1559
    is_eip1559: bool = False


class GasConfig:
    """Конфигурация лимитов газа."""

    # Максимальная стоимость газа за одну sweep транзакцию (в USD эквиваленте ~$5)
    # Выражаем в нативной валюте: ~0.002 ETH, ~0.02 BNB, ~0.1 AVAX, ~0.5 MATIC
    MAX_GAS_COST_WEI = {
        "base": int(0.002 * 10**18),  # 0.002 ETH
        "arbitrum": int(0.002 * 10**18),  # 0.002 ETH
        "optimism": int(0.002 * 10**18),  # 0.002 ETH
        "bsc": int(0.02 * 10**18),  # 0.02 BNB
        "polygon": int(1.0 * 10**18),  # 1 MATIC
        "avax": int(0.2 * 10**18),  # 0.2 AVAX
    }

    # Минимальный запас газа (10%) для непредвиденных расходов
    GAS_BUFFER_PERCENT = 10

    # Максимальное количество повторных попыток дополнения газа
    MAX_GAS_TOP_UP_RETRIES = 3


class Sweeper:
    """Класс для sweep операций."""

    def __init__(self, treasury_address: str, encryption_key: str):
        self.treasury_address = treasury_address
        self.encryption_key = encryption_key
        self._adapters: dict[str, EvmAdapter] = {}

    def get_adapter(self, chain: str) -> Optional[EvmAdapter]:
        """Получить адаптер для сети по имени."""
        if chain not in CHAINS_CONFIG:
            return None

        if chain not in self._adapters:
            self._adapters[chain] = get_evm_adapter(chain)

        return self._adapters[chain]

    async def get_deposit_private_key(self, deposit: DepositAddress) -> Optional[str]:
        """
        Получить приватный ключ для deposit адреса.
        """
        if deposit.encrypted_privkey:
            return decrypt_private_key(deposit.encrypted_privkey, self.encryption_key)

        # Derive из HD Wallet (legacy support)
        settings = get_settings()
        wallet = HDWallet(settings.hd_wallet_seed.get_secret_value())
        _, private_key = wallet.derive_address(deposit.derivation_index)
        return private_key

    async def get_precise_gas_estimate(
        self,
        adapter: EvmAdapter,
        chain: str,
        token_contract: str,
        from_address: str,
        to_address: str,
        amount_raw: int,
    ) -> GasEstimate | None:
        """
        Получить точную оценку газа для ERC20 transfer.

        Использует актуальные данные из блокчейна:
        - estimate_gas для точного количества gas units
        - fee_history для актуальной цены газа (EIP-1559 или legacy)

        Returns:
            GasEstimate с точными параметрами или None при ошибке
        """
        try:
            # 1. Оценка gas units
            gas_units = await adapter.estimate_gas_for_erc20_transfer(
                token_contract, from_address, to_address, amount_raw
            )
            if gas_units is None:
                # Fallback на типичное значение для ERC20 transfer
                gas_units = 65000
                logger.warning(f"[{chain}] Using fallback gas estimate: {gas_units}")

            # 2. Добавляем буфер для безопасности
            # L2 сети (Base, Optimism, Arbitrum) требуют больший буфер
            l2_chains = {"base", "optimism", "arbitrum"}
            if chain in l2_chains:
                gas_buffer = 1.80  # 80% буфер для L2
            elif chain == "bsc":
                gas_buffer = 1.50  # 50% для BSC
            else:
                gas_buffer = 1.25  # 25% для остальных

            gas_units = int(gas_units * gas_buffer)

            # 3. Получаем актуальные fee параметры
            fee_params = await adapter.get_fee_params()

            if fee_params.is_eip1559:
                # EIP-1559: учитываем base fee + priority fee
                # L2 требуют больший буфер из-за волатильности
                if chain in l2_chains:
                    fee_buffer = 1.50  # 50% для L2
                else:
                    fee_buffer = 1.25  # 25% для остальных

                max_fee = int(fee_params.max_fee_per_gas * fee_buffer)
                priority_fee = fee_params.max_priority_fee_per_gas

                # Стоимость = gas_units * max_fee_per_gas
                total_cost = gas_units * max_fee

                return GasEstimate(
                    gas_units=gas_units,
                    gas_price=max_fee,
                    total_cost_wei=total_cost,
                    max_fee_per_gas=max_fee,
                    max_priority_fee=priority_fee,
                    is_eip1559=True,
                )
            else:
                # Legacy: gas_price с буфером (50% для BSC из-за волатильности)
                gas_multiplier = 1.50 if chain == "bsc" else 1.25
                gas_price = int(fee_params.gas_price * gas_multiplier)
                total_cost = gas_units * gas_price

                return GasEstimate(
                    gas_units=gas_units,
                    gas_price=gas_price,
                    total_cost_wei=total_cost,
                    is_eip1559=False,
                )

        except Exception as e:
            logger.error(f"[{chain}] Failed to estimate gas: {e}")
            return None

    async def fund_gas(
        self,
        sweep: SweepJob,
        payment_session: PaymentSession,
        deposit: DepositAddress,
        invoice: Invoice,
        funder_key: str,
        cached_deposit_balance: int | None = None,
    ) -> tuple[bool, int | None]:
        """
        Отправить газ на deposit адрес для sweep транзакции.

        Динамически рассчитывает необходимый газ:
        1. Получает точную оценку gas units
        2. Получает актуальную цену газа
        3. Проверяет лимиты (максимальная стоимость)
        4. Дополняет газ если недостаточно

        Returns:
            Tuple (success, deposit_balance_wei)
        """
        chain = payment_session.chain
        adapter = self.get_adapter(chain)
        if not adapter:
            logger.error(f"Sweep {sweep.id}: no adapter for chain {chain}")
            return False, None

        # Получаем token contract
        token_contract = get_token_contract(chain, payment_session.token)
        if not token_contract:
            logger.error(
                f"Sweep {sweep.id}: no token contract for {chain}/{payment_session.token}"
            )
            return False, None

        # 1. Получаем точную оценку газа
        raw_amount = to_raw_amount(invoice.amount, chain, payment_session.token)
        gas_estimate = await self.get_precise_gas_estimate(
            adapter,
            chain,
            token_contract,
            deposit.address,
            self.treasury_address,
            raw_amount,
        )

        if gas_estimate is None:
            logger.error(f"Sweep {sweep.id}: failed to estimate gas")
            return False, None

        gas_cost = gas_estimate.total_cost_wei

        # 2. Проверяем лимит максимальной стоимости газа
        max_gas_cost = GasConfig.MAX_GAS_COST_WEI.get(chain, int(0.01 * 10**18))
        if gas_cost > max_gas_cost:
            logger.warning(
                f"Sweep {sweep.id}: gas cost {gas_cost} exceeds max {max_gas_cost} for {chain}. "
                f"Capping to max."
            )
            gas_cost = max_gas_cost

        logger.info(
            f"Sweep {sweep.id}: gas estimate for {chain}: "
            f"units={gas_estimate.gas_units}, price={gas_estimate.gas_price}, "
            f"total={gas_cost} wei (~{gas_cost / 10**18:.6f} {get_chain_config(chain).native_symbol})"
        )

        # 3. Проверяем баланс funder
        try:
            funder_address = adapter.private_key_to_address(funder_key)
            funder_balance = await adapter.get_native_balance_wei(funder_address)
        except Exception as e:
            logger.error(f"Sweep {sweep.id}: failed to check funder balance: {e}")
            return False, None

        if funder_balance < gas_cost:
            logger.error(
                f"Sweep {sweep.id}: funder has insufficient balance. "
                f"Need {gas_cost} wei, have {funder_balance} wei"
            )
            return False, None

        # 4. Получаем текущий баланс газа на deposit адресе
        if cached_deposit_balance is not None:
            deposit_balance = cached_deposit_balance
        else:
            try:
                deposit_balance = await adapter.get_native_balance_wei(deposit.address)
            except Exception as e:
                logger.error(f"Sweep {sweep.id}: failed to check deposit balance: {e}")
                return False, None

        # 5. Проверяем нужно ли дополнить газ
        if deposit_balance >= gas_cost:
            logger.info(
                f"Sweep {sweep.id}: deposit already funded "
                f"(have {deposit_balance} wei, need {gas_cost} wei)"
            )
            return True, deposit_balance

        # 6. Вычисляем сколько нужно дополнить
        fund_amount = gas_cost - deposit_balance

        logger.info(
            f"Sweep {sweep.id}: funding {fund_amount} wei "
            f"(deposit has {deposit_balance} wei, need {gas_cost} wei)"
        )

        # 7. Отправляем газ
        try:
            tx_hash = await adapter.send_native_transfer(
                funder_key,
                deposit.address,
                fund_amount,
            )

            if tx_hash:
                sweep.gas_tx_hash = tx_hash
                logger.info(f"Sweep {sweep.id}: gas funding sent, tx={tx_hash}")
                return True, deposit_balance + fund_amount

        except Exception as e:
            logger.error(f"Sweep {sweep.id}: gas funding failed: {e}")

        return False, deposit_balance

    async def check_and_top_up_gas(
        self,
        sweep: SweepJob,
        payment_session: PaymentSession,
        deposit: DepositAddress,
        invoice: Invoice,
        funder_key: str,
    ) -> bool:
        """
        Проверить достаточность газа и дополнить при необходимости.

        Используется перед execute_sweep для защиты от ситуации
        когда цена газа выросла после первоначального funding.

        Returns:
            True если газа достаточно (или успешно дополнен)
        """
        chain = payment_session.chain
        adapter = self.get_adapter(chain)
        if not adapter:
            return False

        token_contract = get_token_contract(chain, payment_session.token)
        if not token_contract:
            return False

        # Получаем актуальную оценку
        raw_amount = to_raw_amount(invoice.amount, chain, payment_session.token)
        gas_estimate = await self.get_precise_gas_estimate(
            adapter,
            chain,
            token_contract,
            deposit.address,
            self.treasury_address,
            raw_amount,
        )

        if gas_estimate is None:
            return False

        gas_cost = gas_estimate.total_cost_wei

        # Проверяем лимит
        max_gas_cost = GasConfig.MAX_GAS_COST_WEI.get(chain, int(0.01 * 10**18))
        gas_cost = min(gas_cost, max_gas_cost)

        # Проверяем текущий баланс
        try:
            deposit_balance = await adapter.get_native_balance_wei(deposit.address)
        except Exception as e:
            logger.error(f"Sweep {sweep.id}: failed to check deposit balance: {e}")
            return False

        if deposit_balance >= gas_cost:
            return True

        # Нужно дополнить
        shortfall = gas_cost - deposit_balance
        logger.warning(
            f"Sweep {sweep.id}: gas price increased, topping up {shortfall} wei"
        )

        try:
            tx_hash = await adapter.send_native_transfer(
                funder_key,
                deposit.address,
                shortfall,
            )
            if tx_hash:
                logger.info(f"Sweep {sweep.id}: gas top-up sent, tx={tx_hash}")
                # Ждём немного для подтверждения
                await asyncio.sleep(3)
                return True
        except Exception as e:
            logger.error(f"Sweep {sweep.id}: gas top-up failed: {e}")

        return False

    async def estimate_sweep_gas_cost(
        self,
        adapter: EvmAdapter,
        sweep: SweepJob,
        payment_session: PaymentSession,
        deposit: DepositAddress,
        invoice: Invoice,
        token_contract: str,
    ) -> int | None:
        """
        Посчитать стоимость газа для sweep.

        DEPRECATED: Используйте get_precise_gas_estimate для новой логики.
        Оставлено для обратной совместимости.
        """
        chain = payment_session.chain
        raw_amount = to_raw_amount(invoice.amount, chain, payment_session.token)

        gas_estimate = await self.get_precise_gas_estimate(
            adapter,
            chain,
            token_contract,
            deposit.address,
            self.treasury_address,
            raw_amount,
        )

        if gas_estimate is None:
            return None

        # Применяем лимит
        max_gas_cost = GasConfig.MAX_GAS_COST_WEI.get(chain, int(0.01 * 10**18))
        return min(gas_estimate.total_cost_wei, max_gas_cost)

    async def execute_sweep(
        self,
        sweep: SweepJob,
        payment_session: PaymentSession,
        deposit: DepositAddress,
        invoice: Invoice,
        deposit_key: str,
    ) -> bool:
        """
        Выполнить sweep транзакцию (перевод токенов на treasury).

        Returns:
            True если транзакция отправлена
        """
        adapter = self.get_adapter(payment_session.chain)
        if not adapter:
            return False

        # Получаем token contract
        token_contract = get_token_contract(
            payment_session.chain, payment_session.token
        )
        if not token_contract:
            return False

        try:
            # Проверяем баланс нативного токена (газа)
            native_balance = await adapter.get_native_balance_wei(deposit.address)
            if native_balance < 10000:  # Минимум 10k wei
                logger.warning(
                    f"Sweep {sweep.id}: insufficient gas on {deposit.address}, "
                    f"balance={native_balance} wei"
                )
                return False

            # Получаем реальный баланс токенов на адресе (в raw units)
            token_balance_raw = await adapter.get_erc20_balance_raw(
                deposit.address, token_contract
            )

            if token_balance_raw <= 0:
                logger.warning(
                    f"Sweep {sweep.id}: no token balance on {deposit.address}"
                )
                return False

            logger.info(
                f"Sweep {sweep.id}: executing sweep, "
                f"token_balance={token_balance_raw}, gas_balance={native_balance} wei"
            )

            tx_hash = await adapter.send_erc20_transfer(
                deposit_key,
                token_contract,
                self.treasury_address,
                token_balance_raw,  # Отправляем raw amount
            )

            if tx_hash:
                sweep.sweep_tx_hash = tx_hash
                logger.info(f"Sweep {sweep.id}: sweep sent, tx={tx_hash}")
                return True

        except Exception as e:
            logger.error(f"Sweep {sweep.id}: sweep failed: {e}", exc_info=True)

        return False

    async def check_tx_confirmed(self, chain: str, tx_hash: str) -> bool:
        """Проверить подтверждение транзакции."""
        if not tx_hash:
            return False

        adapter = self.get_adapter(chain)
        if not adapter:
            return False

        try:
            receipt = await adapter.get_transaction_receipt(tx_hash)
            if receipt and receipt.get("status") == 1:
                return True
        except Exception as e:
            logger.error(f"Failed to check tx {tx_hash}: {e}")

        return False


async def process_sweep_jobs(sweeper: Sweeper, funder_key: str) -> int:
    """
    Обработать sweep jobs.

    Returns:
        Количество обработанных jobs
    """
    processed = 0

    async with get_session_context() as session:
        # 1. Получаем pending_gas jobs - нужно отправить газ
        stmt = (
            select(SweepJob)
            .options(
                selectinload(SweepJob.payment_session).selectinload(
                    PaymentSession.deposit_address
                ),
                selectinload(SweepJob.payment_session).selectinload(
                    PaymentSession.invoice
                ),
            )
            .where(SweepJob.state == SweepState.PENDING_GAS)
            .limit(10)
        )
        result = await session.execute(stmt)
        pending_gas_jobs = result.scalars().all()

        for sweep in pending_gas_jobs:
            # Distributed lock для предотвращения параллельной обработки
            async with redis_lock(f"sweep:job:{sweep.id}", timeout=60) as acquired:
                if not acquired:
                    logger.debug(
                        f"Sweep {sweep.id} is being processed by another worker"
                    )
                    continue

                ps = sweep.payment_session
                deposit = ps.deposit_address
                invoice = ps.invoice

                success, _ = await sweeper.fund_gas(
                    sweep, ps, deposit, invoice, funder_key
                )
                if success:
                    sweep.state = SweepState.FUNDING
                else:
                    sweep.attempts += 1
                    sweep.next_retry_at = datetime.now(timezone.utc) + timedelta(
                        minutes=5
                    )
                    if sweep.attempts >= sweep.max_attempts:
                        sweep.state = SweepState.FAILED
                        sweep.last_error = "Gas funding failed after max retries"
                processed += 1

        # 2. Получаем funding jobs - проверяем подтверждение газа
        stmt = (
            select(SweepJob)
            .options(
                selectinload(SweepJob.payment_session).selectinload(
                    PaymentSession.deposit_address
                ),
                selectinload(SweepJob.payment_session).selectinload(
                    PaymentSession.invoice
                ),
            )
            .where(SweepJob.state == SweepState.FUNDING)
            .limit(10)
        )
        result = await session.execute(stmt)
        funding_jobs = result.scalars().all()

        for sweep in funding_jobs:
            # Distributed lock для предотвращения параллельной обработки
            async with redis_lock(f"sweep:job:{sweep.id}", timeout=60) as acquired:
                if not acquired:
                    logger.debug(
                        f"Sweep {sweep.id} is being processed by another worker"
                    )
                    continue

                ps = sweep.payment_session
                deposit = ps.deposit_address
                invoice = ps.invoice

                if not sweep.gas_tx_hash:
                    adapter = sweeper.get_adapter(ps.chain)
                    token_contract = get_token_contract(ps.chain, ps.token)
                    if adapter and token_contract:
                        gas_cost = await sweeper.estimate_sweep_gas_cost(
                            adapter, sweep, ps, deposit, invoice, token_contract
                        )
                        if gas_cost is not None:
                            try:
                                deposit_balance_wei = (
                                    await adapter.get_native_balance_wei(
                                        deposit.address
                                    )
                                )
                            except Exception as e:
                                logger.error(
                                    f"Sweep {sweep.id}: failed to check deposit balance: {e}"
                                )
                                processed += 1
                                continue

                            if deposit_balance_wei < gas_cost:
                                processed += 1
                                continue
                    else:
                        processed += 1
                        continue
                else:
                    confirmed = await sweeper.check_tx_confirmed(
                        ps.chain, sweep.gas_tx_hash
                    )
                    if not confirmed:
                        processed += 1
                        continue

                # Газ подтверждён - проверяем достаточность и дополняем при необходимости
                deposit_key = await sweeper.get_deposit_private_key(deposit)

                if not deposit_key:
                    sweep.state = SweepState.FAILED
                    sweep.last_error = "Failed to get deposit private key"
                    processed += 1
                    continue

                # Проверяем и дополняем газ если цена выросла
                gas_ok = await sweeper.check_and_top_up_gas(
                    sweep, ps, deposit, invoice, funder_key
                )

                if not gas_ok:
                    logger.warning(f"Sweep {sweep.id}: gas top-up failed, will retry")
                    sweep.attempts += 1
                    sweep.next_retry_at = datetime.now(timezone.utc) + timedelta(
                        minutes=2
                    )
                    if sweep.attempts >= sweep.max_attempts:
                        sweep.state = SweepState.FAILED
                        sweep.last_error = "Gas top-up failed after max retries"
                    processed += 1
                    continue

                # Выполняем sweep
                success = await sweeper.execute_sweep(
                    sweep, ps, deposit, invoice, deposit_key
                )
                if success:
                    sweep.state = SweepState.SWEEPING
                else:
                    sweep.attempts += 1
                    sweep.next_retry_at = datetime.now(timezone.utc) + timedelta(
                        minutes=5
                    )
                    if sweep.attempts >= sweep.max_attempts:
                        sweep.state = SweepState.FAILED
                        sweep.last_error = "Sweep execution failed"
                processed += 1

        # 3. Получаем sweeping jobs - проверяем подтверждение sweep
        stmt = (
            select(SweepJob)
            .options(
                selectinload(SweepJob.payment_session),
            )
            .where(SweepJob.state == SweepState.SWEEPING)
            .limit(10)
        )
        result = await session.execute(stmt)
        sweeping_jobs = result.scalars().all()

        for sweep in sweeping_jobs:
            # Distributed lock для предотвращения параллельной обработки
            async with redis_lock(f"sweep:job:{sweep.id}", timeout=60) as acquired:
                if not acquired:
                    logger.debug(
                        f"Sweep {sweep.id} is being processed by another worker"
                    )
                    continue

                ps = sweep.payment_session
                confirmed = await sweeper.check_tx_confirmed(
                    ps.chain, sweep.sweep_tx_hash
                )
                if confirmed:
                    sweep.state = SweepState.COMPLETED
                    logger.info(f"Sweep {sweep.id} completed successfully")
                processed += 1

        await session.commit()

    return processed


async def run_sweeper() -> None:
    """
    Главный цикл Sweeper worker.
    """
    settings = get_settings()

    if not settings.treasury_address:
        logger.warning("TREASURY_ADDRESS not configured, sweeper disabled")
        while True:
            await asyncio.sleep(60)

    if not settings.funder_private_key.get_secret_value():
        logger.warning("FUNDER_PRIVATE_KEY not configured, sweeper disabled")
        while True:
            await asyncio.sleep(60)

    sweeper = Sweeper(
        treasury_address=settings.treasury_address,
        encryption_key=settings.encryption_key.get_secret_value(),
    )

    logger.info("Starting Sweeper worker")

    while True:
        try:
            processed = await process_sweep_jobs(
                sweeper,
                settings.funder_private_key.get_secret_value(),
            )
            if processed > 0:
                logger.info(f"Processed {processed} sweep jobs")
        except Exception as e:
            logger.error(f"Error processing sweep jobs: {e}")

        # Пауза между итерациями (15 секунд)
        await asyncio.sleep(15)


# ARQ Worker Settings
class WorkerSettings:
    """Настройки для ARQ worker."""

    @staticmethod
    async def run_worker():
        """Запуск worker."""
        await run_sweeper()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(run_sweeper())
