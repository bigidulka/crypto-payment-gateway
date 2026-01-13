"""
Batch Sweeper - оптимизированный вывод средств со всех кошельков.

Стратегия максимальной оптимизации:
1. Multicall3 для проверки балансов (1 RPC вызов на сеть)
2. Параллельная обработка всех сетей (asyncio.gather)
3. Группировка транзакций по nonce для одного funder
4. Фильтрация только кошельков с балансом > dust threshold
5. Приоритизация по сумме (сначала большие балансы)
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_chain_config, get_all_chains, is_chain_supported
from src.blockchain.evm_adapter import get_evm_adapter, EvmAdapter
from src.core.config import get_settings
from src.crypto.encryption import decrypt_private_key
from src.db.models import (
    DepositAddress,
    PaymentSession,
    WalletAddress,
    SweepJob,
    SweepState,
)
from src.db.session import get_session_context

logger = logging.getLogger(__name__)


# Минимальный баланс для sweep (в долларах, примерно)
# Игнорируем dust балансы чтобы не тратить газ впустую
DUST_THRESHOLD_USD = Decimal("0.50")
DUST_THRESHOLD_RAW = {
    6: 500_000,  # USDT/USDC (6 decimals) = $0.50
    18: 500_000_000_000_000_000,  # 0.5 токена (18 decimals)
}


@dataclass
class WalletBalance:
    """Баланс кошелька для sweep."""

    address: str
    chain: str
    token: str
    token_contract: str
    balance_raw: int
    balance: Decimal
    native_balance_wei: int
    encrypted_privkey: str
    wallet_type: str  # 'deposit' | 'persistent'
    source_id: str  # PaymentSession ID или WalletAddress ID


@dataclass
class SweepPlan:
    """План sweep операции."""

    wallet: WalletBalance
    estimated_gas_wei: int
    needs_gas_funding: bool
    gas_shortfall_wei: int
    priority: int  # Чем выше баланс, тем выше приоритет


@dataclass
class ChainSweepResult:
    """Результат sweep для одной сети."""

    chain: str
    total_wallets: int
    swept_count: int
    total_amount: Decimal
    failed_count: int
    gas_spent_wei: int
    duration_sec: float


class BatchSweeper:
    """
    Оптимизированный batch sweeper.

    Workflow:
    1. collect_all_balances() - собираем балансы со всех кошельков
    2. create_sweep_plan() - создаём план с приоритизацией
    3. execute_sweep_plan() - выполняем sweep параллельно по сетям
    """

    def __init__(self):
        self.settings = get_settings()
        self.treasury = self.settings.treasury_address
        self.encryption_key = self.settings.encryption_key.get_secret_value()
        self._adapters: dict[str, EvmAdapter] = {}

    def get_adapter(self, chain: str) -> EvmAdapter:
        """Получить или создать адаптер для сети."""
        if chain not in self._adapters:
            self._adapters[chain] = get_evm_adapter(chain)
        return self._adapters[chain]

    async def collect_all_balances(
        self,
        chains: list[str] | None = None,
        include_deposits: bool = True,
        include_persistent: bool = True,
    ) -> list[WalletBalance]:
        """
        Собрать балансы со всех кошельков.

        Использует Multicall3 для batch запросов балансов.
        Параллельно обрабатывает все сети.

        Args:
            chains: Список сетей (None = все)
            include_deposits: Включить deposit адреса (инвойсы)
            include_persistent: Включить persistent адреса

        Returns:
            Список WalletBalance с балансом > dust threshold
        """
        if chains is None:
            chains = list(get_all_chains())

        # Параллельно собираем балансы со всех сетей
        tasks = [
            self._collect_chain_balances(chain, include_deposits, include_persistent)
            for chain in chains
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_balances: list[WalletBalance] = []
        for chain, result in zip(chains, results):
            if isinstance(result, Exception):
                logger.error(f"[{chain}] Error collecting balances: {result}")
                continue
            all_balances.extend(result)

        # Сортируем по балансу (большие первые)
        all_balances.sort(key=lambda w: w.balance_raw, reverse=True)

        logger.info(
            f"Collected {len(all_balances)} wallets with balance > dust threshold "
            f"across {len(chains)} chains"
        )

        return all_balances

    async def _collect_chain_balances(
        self,
        chain: str,
        include_deposits: bool,
        include_persistent: bool,
    ) -> list[WalletBalance]:
        """Собрать балансы для одной сети."""
        config = get_chain_config(chain)
        adapter = self.get_adapter(chain)

        wallets: list[WalletBalance] = []

        async with get_session_context() as session:
            # Собираем адреса
            addresses_info: dict[str, dict] = {}  # address -> info

            if include_deposits:
                # Deposit адреса (инвойсы)
                stmt = (
                    select(PaymentSession)
                    .options(selectinload(PaymentSession.deposit_address))
                    .where(
                        and_(
                            PaymentSession.chain == chain,
                            PaymentSession.deposit_address_id.isnot(None),
                        )
                    )
                )
                result = await session.execute(stmt)
                for ps in result.scalars().unique():
                    if ps.deposit_address:
                        addr = ps.deposit_address.address.lower()
                        addresses_info[addr] = {
                            "type": "deposit",
                            "encrypted_privkey": ps.deposit_address.encrypted_privkey,
                            "source_id": str(ps.id),
                        }

            if include_persistent:
                # Persistent адреса
                stmt = select(WalletAddress).where(
                    and_(
                        WalletAddress.chain == chain,
                        WalletAddress.is_active == True,
                    )
                )
                result = await session.execute(stmt)
                for wa in result.scalars():
                    addr = wa.address.lower()
                    if addr not in addresses_info:  # Избегаем дубликатов
                        addresses_info[addr] = {
                            "type": "persistent",
                            "encrypted_privkey": wa.encrypted_private_key,
                            "source_id": str(wa.id),
                        }

            if not addresses_info:
                return []

            addresses = list(addresses_info.keys())

            # Batch запрос балансов через Multicall3
            token_contracts = [
                config.tokens["USDT"].contract_address,
                config.tokens["USDC"].contract_address,
            ]

            try:
                # Параллельно получаем токен балансы и native балансы
                token_balances_task = adapter.get_balances_batch(
                    addresses, token_contracts
                )
                native_balances_task = adapter.get_native_balances_batch(addresses)

                token_balances, native_balances = await asyncio.gather(
                    token_balances_task, native_balances_task
                )
            except Exception as e:
                logger.error(f"[{chain}] Failed to fetch balances: {e}")
                return []

            # Формируем результат
            for addr, info in addresses_info.items():
                addr_token_balances = token_balances.get(addr, {})
                native_wei = native_balances.get(addr, Decimal(0))
                native_wei_int = int(native_wei * 10**18)

                for token_symbol, token_config in config.tokens.items():
                    contract_addr = token_config.contract_address.lower()
                    balance_decimal = addr_token_balances.get(contract_addr, Decimal(0))
                    balance_raw = int(balance_decimal * 10**token_config.decimals)

                    # Проверяем dust threshold
                    dust = DUST_THRESHOLD_RAW.get(token_config.decimals, 500_000)
                    if balance_raw <= dust:
                        continue

                    wallets.append(
                        WalletBalance(
                            address=addr,
                            chain=chain,
                            token=token_symbol,
                            token_contract=token_config.contract_address,
                            balance_raw=balance_raw,
                            balance=balance_decimal,
                            native_balance_wei=native_wei_int,
                            encrypted_privkey=info["encrypted_privkey"],
                            wallet_type=info["type"],
                            source_id=info["source_id"],
                        )
                    )

        logger.debug(f"[{chain}] Found {len(wallets)} wallets with balance")
        return wallets

    async def create_sweep_plan(
        self,
        wallets: list[WalletBalance],
    ) -> list[SweepPlan]:
        """
        Создать план sweep с оценкой газа.

        Для каждого кошелька:
        1. Оцениваем газ для ERC20 transfer
        2. Проверяем, хватает ли native баланса
        3. Рассчитываем необходимое дополнение газа
        """
        plans: list[SweepPlan] = []

        # Группируем по сетям для batch оценки газа
        by_chain: dict[str, list[WalletBalance]] = {}
        for w in wallets:
            by_chain.setdefault(w.chain, []).append(w)

        # Параллельно оцениваем газ по сетям
        async def estimate_chain_gas(chain: str, chain_wallets: list[WalletBalance]):
            adapter = self.get_adapter(chain)
            config = get_chain_config(chain)
            chain_plans = []

            # Типичный газ для ERC20 transfer
            try:
                fee_params = await adapter.get_fee_params()
                if fee_params.is_eip1559:
                    gas_price = int(fee_params.max_fee_per_gas * 1.25)
                else:
                    gas_price = int(fee_params.gas_price * 1.25)
            except:
                gas_price = 50_000_000_000  # 50 gwei fallback

            # ~65k gas для ERC20 transfer с буфером
            base_gas_units = 80_000
            estimated_gas_wei = base_gas_units * gas_price

            for w in chain_wallets:
                needs_funding = w.native_balance_wei < estimated_gas_wei
                shortfall = max(0, estimated_gas_wei - w.native_balance_wei)

                chain_plans.append(
                    SweepPlan(
                        wallet=w,
                        estimated_gas_wei=estimated_gas_wei,
                        needs_gas_funding=needs_funding,
                        gas_shortfall_wei=shortfall,
                        priority=w.balance_raw,
                    )
                )

            return chain_plans

        tasks = [estimate_chain_gas(chain, ws) for chain, ws in by_chain.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error estimating gas: {result}")
                continue
            plans.extend(result)

        # Сортируем по приоритету (большие балансы первые)
        plans.sort(key=lambda p: p.priority, reverse=True)

        # Статистика
        needs_gas = sum(1 for p in plans if p.needs_gas_funding)
        total_gas_needed = sum(p.gas_shortfall_wei for p in plans)

        logger.info(
            f"Sweep plan: {len(plans)} wallets, "
            f"{needs_gas} need gas funding, "
            f"total gas needed: {total_gas_needed / 10**18:.6f} native"
        )

        return plans

    async def execute_sweep_plan(
        self,
        plans: list[SweepPlan],
        dry_run: bool = False,
        max_concurrent_per_chain: int = 5,
    ) -> dict[str, ChainSweepResult]:
        """
        Выполнить план sweep.

        Оптимизации:
        1. Параллельная обработка сетей
        2. Semaphore для ограничения concurrent транзакций
        3. Групповая отправка газа (один funder nonce per batch)

        Args:
            plans: Список SweepPlan
            dry_run: Только симуляция без реальных транзакций
            max_concurrent_per_chain: Макс. параллельных sweep на сеть

        Returns:
            Результаты по сетям
        """
        # Группируем по сетям
        by_chain: dict[str, list[SweepPlan]] = {}
        for p in plans:
            by_chain.setdefault(p.wallet.chain, []).append(p)

        # Параллельно выполняем sweep по сетям
        tasks = [
            self._execute_chain_sweep(
                chain, chain_plans, dry_run, max_concurrent_per_chain
            )
            for chain, chain_plans in by_chain.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        chain_results: dict[str, ChainSweepResult] = {}
        for chain, result in zip(by_chain.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"[{chain}] Sweep failed: {result}")
                chain_results[chain] = ChainSweepResult(
                    chain=chain,
                    total_wallets=len(by_chain[chain]),
                    swept_count=0,
                    total_amount=Decimal(0),
                    failed_count=len(by_chain[chain]),
                    gas_spent_wei=0,
                    duration_sec=0,
                )
            else:
                chain_results[chain] = result

        # Итоговая статистика
        total_swept = sum(r.swept_count for r in chain_results.values())
        total_amount = sum(r.total_amount for r in chain_results.values())
        total_failed = sum(r.failed_count for r in chain_results.values())

        logger.info(
            f"Batch sweep complete: {total_swept} swept, {total_failed} failed, "
            f"total amount: ${total_amount:.2f}"
        )

        return chain_results

    async def _execute_chain_sweep(
        self,
        chain: str,
        plans: list[SweepPlan],
        dry_run: bool,
        max_concurrent: int,
    ) -> ChainSweepResult:
        """Выполнить sweep для одной сети."""
        import time

        start_time = time.time()

        adapter = self.get_adapter(chain)
        funder_key = self.settings.funder_private_key.get_secret_value()

        swept_count = 0
        failed_count = 0
        total_amount = Decimal(0)
        gas_spent = 0

        semaphore = asyncio.Semaphore(max_concurrent)

        async def sweep_one(plan: SweepPlan) -> bool:
            async with semaphore:
                try:
                    wallet = plan.wallet

                    if dry_run:
                        logger.info(
                            f"[{chain}] DRY RUN: Would sweep {wallet.balance} {wallet.token} "
                            f"from {wallet.address[:10]}..."
                        )
                        return True

                    # 1. Отправляем газ если нужно
                    if plan.needs_gas_funding:
                        try:
                            tx_hash = await adapter.send_native_transfer(
                                funder_key,
                                wallet.address,
                                plan.gas_shortfall_wei,
                            )
                            logger.debug(
                                f"[{chain}] Gas sent to {wallet.address[:10]}...: {tx_hash}"
                            )
                            # Ждём подтверждения газа
                            await asyncio.sleep(3)
                        except Exception as e:
                            logger.error(
                                f"[{chain}] Gas funding failed for {wallet.address[:10]}...: {e}"
                            )
                            return False

                    # 2. Получаем актуальный баланс токена (выводим ВСЁ под ноль)
                    try:
                        actual_balance_raw = await adapter.get_erc20_balance_raw(
                            wallet.address, wallet.token_contract
                        )
                        if actual_balance_raw <= 0:
                            logger.warning(
                                f"[{chain}] No balance to sweep on {wallet.address[:10]}..."
                            )
                            return False
                    except Exception as e:
                        logger.error(
                            f"[{chain}] Failed to get actual balance for {wallet.address[:10]}...: {e}"
                        )
                        # Fallback на сохранённый баланс
                        actual_balance_raw = wallet.balance_raw

                    # 3. Выполняем sweep
                    # encrypted_privkey может быть bytes или hex string
                    encrypted_data = wallet.encrypted_privkey
                    if isinstance(encrypted_data, str):
                        encrypted_data = bytes.fromhex(encrypted_data)
                    
                    privkey = decrypt_private_key(
                        encrypted_data, self.encryption_key
                    )

                    tx_hash = await adapter.send_erc20_transfer(
                        from_private_key=privkey,
                        token_contract=wallet.token_contract,
                        to_address=self.treasury,
                        amount=actual_balance_raw,
                    )

                    if tx_hash:
                        # Логируем реальную сумму
                        config = get_chain_config(chain)
                        token_config = config.tokens.get(wallet.token)
                        decimals = token_config.decimals if token_config else 18
                        actual_amount = Decimal(actual_balance_raw) / Decimal(10 ** decimals)
                        
                        logger.info(
                            f"[{chain}] Swept {actual_amount} {wallet.token} "
                            f"from {wallet.address[:10]}... tx={tx_hash[:16]}..."
                        )
                        return True

                    return False

                except Exception as e:
                    logger.error(
                        f"[{chain}] Sweep failed for {plan.wallet.address[:10]}...: {e}"
                    )
                    return False

        # Выполняем все sweep параллельно (с ограничением semaphore)
        results = await asyncio.gather(
            *[sweep_one(p) for p in plans],
            return_exceptions=True,
        )

        for plan, result in zip(plans, results):
            if result is True:
                swept_count += 1
                total_amount += plan.wallet.balance
                gas_spent += plan.estimated_gas_wei
            else:
                failed_count += 1

        duration = time.time() - start_time

        return ChainSweepResult(
            chain=chain,
            total_wallets=len(plans),
            swept_count=swept_count,
            total_amount=total_amount,
            failed_count=failed_count,
            gas_spent_wei=gas_spent,
            duration_sec=duration,
        )


async def run_batch_sweep(
    chains: list[str] | None = None,
    dry_run: bool = False,
    include_deposits: bool = True,
    include_persistent: bool = True,
) -> dict[str, ChainSweepResult]:
    """
    Запустить batch sweep.

    Пример использования:

    ```python
    from src.workers.batch_sweeper import run_batch_sweep

    # Dry run для проверки
    results = await run_batch_sweep(dry_run=True)

    # Реальный sweep
    results = await run_batch_sweep()

    # Только BSC и Polygon
    results = await run_batch_sweep(chains=['bsc', 'polygon'])

    # Только persistent кошельки
    results = await run_batch_sweep(include_deposits=False, include_persistent=True)
    ```
    """
    sweeper = BatchSweeper()

    # 1. Собираем балансы
    logger.info("Starting batch sweep - collecting balances...")
    wallets = await sweeper.collect_all_balances(
        chains=chains,
        include_deposits=include_deposits,
        include_persistent=include_persistent,
    )

    if not wallets:
        logger.info("No wallets with balance found")
        return {}

    # 2. Создаём план
    logger.info("Creating sweep plan...")
    plans = await sweeper.create_sweep_plan(wallets)

    if not plans:
        logger.info("No sweep plans created")
        return {}

    # 3. Выполняем
    logger.info(f"Executing sweep plan ({len(plans)} wallets)...")
    results = await sweeper.execute_sweep_plan(plans, dry_run=dry_run)

    return results


# CLI entry point
if __name__ == "__main__":
    import sys

    async def main():
        dry_run = "--dry-run" in sys.argv or "-n" in sys.argv

        if dry_run:
            print("🔍 DRY RUN MODE - no real transactions will be sent")
        else:
            print("⚠️  REAL MODE - transactions will be sent!")
            print("Press Ctrl+C within 5 seconds to cancel...")
            try:
                await asyncio.sleep(5)
            except KeyboardInterrupt:
                print("\nCancelled.")
                return

        results = await run_batch_sweep(dry_run=dry_run)

        print("\n" + "=" * 60)
        print("BATCH SWEEP RESULTS")
        print("=" * 60)

        for chain, result in results.items():
            print(f"\n{chain.upper()}:")
            print(f"  Swept: {result.swept_count}/{result.total_wallets}")
            print(f"  Total: ${result.total_amount:.2f}")
            print(f"  Failed: {result.failed_count}")
            print(f"  Duration: {result.duration_sec:.1f}s")

    asyncio.run(main())
