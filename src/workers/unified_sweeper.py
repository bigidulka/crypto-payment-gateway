"""
Unified Batch Sweeper - единый sweep worker.

Обрабатывает UnifiedSweepJob из очереди.
НЕ сканирует адреса - только берёт готовые задачи.

Workflow:
1. Получаем PENDING_GAS jobs из БД
2. Группируем по chain для оптимизации
3. Проверяем нужен ли газ (Multicall3 batch)
4. Отправляем газ если нужно
5. Выполняем sweep
6. Обновляем статус
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.blockchain.chains import get_chain_config, get_evm_chains
from src.blockchain.evm_adapter import get_evm_adapter, EvmAdapter
from src.core.config import get_settings
from src.crypto.encryption import decrypt_private_key
from src.db.models import UnifiedSweepJob, SweepState
from src.db.session import get_session_context
from src.db.redis import redis_lock

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """Результат sweep одного job."""
    job_id: str
    success: bool
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    gas_sent: bool = False


@dataclass
class BatchResult:
    """Результат batch sweep."""
    chain: str
    total_jobs: int
    completed: int
    failed: int
    total_amount: Decimal
    duration_sec: float


class UnifiedBatchSweeper:
    """
    Единый batch sweeper.
    
    Обрабатывает только созданные UnifiedSweepJob.
    Не сканирует адреса самостоятельно.
    """

    def __init__(self):
        self.settings = get_settings()
        self.encryption_key = self.settings.encryption_key.get_secret_value()
        self.funder_key = self.settings.funder_private_key.get_secret_value()
        self._adapters: dict[str, EvmAdapter] = {}

    def get_adapter(self, chain: str) -> EvmAdapter:
        """Получить или создать адаптер для сети."""
        if chain not in self._adapters:
            self._adapters[chain] = get_evm_adapter(chain)
        return self._adapters[chain]

    async def process_pending_jobs(
        self,
        max_jobs_per_chain: int = 20,
        dry_run: bool = False,
    ) -> dict[str, BatchResult]:
        """
        Обработать pending sweep jobs.
        
        Args:
            max_jobs_per_chain: Максимум jobs на сеть за итерацию
            dry_run: Только логировать, не выполнять
            
        Returns:
            Результаты по сетям
        """
        results: dict[str, BatchResult] = {}
        
        async with get_session_context() as session:
            # Получаем jobs сгруппированные по chain
            jobs_by_chain = await self._get_pending_jobs_by_chain(
                session, max_jobs_per_chain
            )
            
            if not jobs_by_chain:
                logger.debug("No pending sweep jobs")
                return results

            # Обрабатываем каждую сеть параллельно
            tasks = [
                self._process_chain_jobs(session, chain, jobs, dry_run)
                for chain, jobs in jobs_by_chain.items()
            ]
            
            chain_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for chain, result in zip(jobs_by_chain.keys(), chain_results):
                if isinstance(result, Exception):
                    logger.error(f"[{chain}] Batch sweep failed: {result}")
                    results[chain] = BatchResult(
                        chain=chain,
                        total_jobs=len(jobs_by_chain[chain]),
                        completed=0,
                        failed=len(jobs_by_chain[chain]),
                        total_amount=Decimal(0),
                        duration_sec=0,
                    )
                else:
                    results[chain] = result
            
            await session.commit()
        
        return results

    async def _get_pending_jobs_by_chain(
        self,
        session: AsyncSession,
        limit_per_chain: int,
    ) -> dict[str, list[UnifiedSweepJob]]:
        """Получить pending jobs сгруппированные по chain."""
        now = datetime.now(timezone.utc)
        
        stmt = (
            select(UnifiedSweepJob)
            .where(
                and_(
                    UnifiedSweepJob.state.in_([
                        SweepState.PENDING_GAS,
                        SweepState.FUNDING,
                        SweepState.SWEEPING,
                    ]),
                    (UnifiedSweepJob.next_retry_at.is_(None)) |
                    (UnifiedSweepJob.next_retry_at <= now),
                )
            )
            .order_by(
                UnifiedSweepJob.priority.desc(),
                UnifiedSweepJob.created_at.asc(),
            )
            .limit(limit_per_chain * 10)  # Получаем больше, потом фильтруем
        )
        
        result = await session.execute(stmt)
        jobs = result.scalars().all()
        
        # Группируем по chain с лимитом
        by_chain: dict[str, list[UnifiedSweepJob]] = {}
        for job in jobs:
            if job.chain not in by_chain:
                by_chain[job.chain] = []
            if len(by_chain[job.chain]) < limit_per_chain:
                by_chain[job.chain].append(job)
        
        return by_chain

    async def _process_chain_jobs(
        self,
        session: AsyncSession,
        chain: str,
        jobs: list[UnifiedSweepJob],
        dry_run: bool,
    ) -> BatchResult:
        """Обработать jobs для одной сети."""
        start_time = time.time()
        adapter = self.get_adapter(chain)
        config = get_chain_config(chain)
        
        completed = 0
        failed = 0
        total_amount = Decimal(0)
        
        logger.info(f"[{chain}] Processing {len(jobs)} sweep jobs")
        
        # Batch получаем native балансы для всех адресов
        addresses = [job.from_address for job in jobs]
        try:
            native_balances = await adapter.get_native_balances_batch(addresses)
        except Exception as e:
            logger.error(f"[{chain}] Failed to get native balances: {e}")
            native_balances = {}
        
        # Обрабатываем каждый job
        for job in jobs:
            # Redis lock для предотвращения дублей
            async with redis_lock(f"sweep:unified:{job.id}", timeout=120) as acquired:
                if not acquired:
                    logger.debug(f"[{chain}] Job {job.id} locked, skipping")
                    continue
                
                try:
                    result = await self._process_single_job(
                        session, adapter, config, job,
                        native_balances.get(job.from_address.lower(), Decimal(0)),
                        dry_run,
                    )
                    
                    if result.success:
                        completed += 1
                        total_amount += job.amount
                    else:
                        failed += 1
                        
                except Exception as e:
                    logger.error(f"[{chain}] Job {job.id} failed: {e}")
                    await self._mark_job_failed(job, str(e))
                    failed += 1
        
        duration = time.time() - start_time
        
        logger.info(
            f"[{chain}] Batch complete: {completed}/{len(jobs)} succeeded, "
            f"total {total_amount} tokens in {duration:.1f}s"
        )
        
        return BatchResult(
            chain=chain,
            total_jobs=len(jobs),
            completed=completed,
            failed=failed,
            total_amount=total_amount,
            duration_sec=duration,
        )

    async def _process_single_job(
        self,
        session: AsyncSession,
        adapter: EvmAdapter,
        config,
        job: UnifiedSweepJob,
        native_balance: Decimal,
        dry_run: bool,
    ) -> SweepResult:
        """Обработать один sweep job."""
        
        if dry_run:
            logger.info(
                f"[{job.chain}] DRY RUN: Would sweep {job.amount} {job.token} "
                f"from {job.from_address[:10]}..."
            )
            return SweepResult(job_id=str(job.id), success=True)
        
        # === State Machine ===
        
        if job.state == SweepState.PENDING_GAS:
            # Проверяем нужен ли газ
            native_wei = int(native_balance * 10**18)
            
            # Оцениваем реальную стоимость sweep
            amount_raw_int = int(Decimal(job.amount_raw)) if job.amount_raw else 1000000
            estimated_cost = await adapter.estimate_erc20_transfer_cost(
                token_contract=job.token_contract,
                from_address=job.from_address,
                to_address=job.to_address,
                amount=amount_raw_int,
            )
            
            # Fallback на max_gas_cost если estimate не удался
            if estimated_cost is None:
                required_gas_wei = config.max_gas_cost_wei
                logger.warning(f"[{job.chain}] Job {job.id}: using fallback max_gas_cost")
            else:
                required_gas_wei = estimated_cost
            
            logger.debug(
                f"[{job.chain}] Job {job.id}: native_wei={native_wei}, required={required_gas_wei}, "
                f"has_enough={native_wei >= required_gas_wei}"
            )
            
            if native_wei >= required_gas_wei:
                # Газа достаточно, переходим к sweep
                job.needs_gas_funding = False
                job.state = SweepState.SWEEPING
                logger.info(f"[{job.chain}] Job {job.id}: enough gas ({native_wei} >= {required_gas_wei}), skipping funding")
            else:
                # Нужно отправить газ - только недостающую сумму
                gas_shortfall = required_gas_wei - native_wei
                
                try:
                    tx_hash = await adapter.send_native_transfer(
                        self.funder_key,
                        job.from_address,
                        gas_shortfall,
                    )
                    job.gas_tx_hash = tx_hash
                    job.state = SweepState.FUNDING
                    job.estimated_gas_wei = str(required_gas_wei)
                    
                    # Логируем в человекочитаемом формате
                    gas_human = Decimal(gas_shortfall) / Decimal(10**18)
                    logger.info(
                        f"[{job.chain}] Job {job.id}: gas sent ({gas_human:.6f} {config.native_symbol}), "
                        f"tx={tx_hash[:16]}..."
                    )
                    
                    # Ждём немного для подтверждения газа
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    await self._mark_job_failed(job, f"Gas funding failed: {e}")
                    return SweepResult(
                        job_id=str(job.id),
                        success=False,
                        error=str(e),
                    )
        
        if job.state == SweepState.FUNDING:
            # Проверяем подтверждён ли газ
            if job.gas_tx_hash:
                try:
                    receipt = await adapter.get_transaction_receipt(job.gas_tx_hash)
                    if receipt and receipt.get("status") == 1:
                        job.state = SweepState.SWEEPING
                        logger.debug(f"[{job.chain}] Job {job.id}: gas confirmed")
                    else:
                        # Ещё не подтверждён, пропускаем
                        return SweepResult(job_id=str(job.id), success=False, error="Gas pending")
                except Exception:
                    # Retry позже
                    return SweepResult(job_id=str(job.id), success=False, error="Gas check failed")
        
        if job.state == SweepState.SWEEPING:
            # Выполняем sweep
            try:
                # Расшифровываем приватный ключ
                encrypted_data = job.encrypted_private_key
                if isinstance(encrypted_data, str):
                    # Может быть hex string или bytes
                    try:
                        encrypted_data = bytes.fromhex(encrypted_data)
                    except ValueError:
                        encrypted_data = encrypted_data.encode()
                
                privkey = decrypt_private_key(encrypted_data, self.encryption_key)
                
                # Получаем актуальный баланс
                actual_balance = await adapter.get_erc20_balance_raw(
                    job.from_address, job.token_contract
                )
                
                if actual_balance <= 0:
                    # Баланс уже 0, возможно уже swept
                    job.state = SweepState.COMPLETED
                    job.completed_at = datetime.now(timezone.utc)
                    logger.warning(f"[{job.chain}] Job {job.id}: balance already 0")
                    return SweepResult(job_id=str(job.id), success=True)
                
                # Отправляем sweep транзакцию
                tx_hash = await adapter.send_erc20_transfer(
                    from_private_key=privkey,
                    token_contract=job.token_contract,
                    to_address=job.to_address,
                    amount=actual_balance,
                )
                
                if tx_hash:
                    job.sweep_tx_hash = tx_hash
                    job.state = SweepState.COMPLETED
                    job.completed_at = datetime.now(timezone.utc)
                    
                    # Логируем реальную сумму
                    token_config = config.tokens.get(job.token)
                    decimals = token_config.decimals if token_config else 18
                    actual_amount = Decimal(actual_balance) / Decimal(10 ** decimals)
                    
                    logger.info(
                        f"[{job.chain}] Swept {actual_amount} {job.token} "
                        f"from {job.from_address[:10]}... tx={tx_hash[:16]}..."
                    )
                    
                    return SweepResult(
                        job_id=str(job.id),
                        success=True,
                        tx_hash=tx_hash,
                    )
                else:
                    # Если sweep вернул None - скорее всего insufficient funds
                    # Возвращаем в PENDING_GAS для дозаправки
                    job.state = SweepState.PENDING_GAS
                    logger.warning(f"[{job.chain}] Job {job.id}: sweep failed, reverting to PENDING_GAS for refunding")
                    return SweepResult(
                        job_id=str(job.id),
                        success=False,
                        error="Sweep tx failed - needs more gas",
                    )
                    
            except Exception as e:
                error_str = str(e).lower()
                # Если ошибка связана с газом - возвращаем в PENDING_GAS
                if "insufficient" in error_str or "gas" in error_str:
                    job.state = SweepState.PENDING_GAS
                    logger.warning(f"[{job.chain}] Job {job.id}: {e}, reverting to PENDING_GAS")
                    return SweepResult(job_id=str(job.id), success=False, error=str(e))
                
                await self._mark_job_failed(job, f"Sweep failed: {e}")
                return SweepResult(
                    job_id=str(job.id),
                    success=False,
                    error=str(e),
                )
        
        return SweepResult(job_id=str(job.id), success=False, error="Unknown state")

    async def _mark_job_failed(
        self,
        job: UnifiedSweepJob,
        error: str,
    ) -> None:
        """Отметить job как failed с retry."""
        job.attempts += 1
        job.last_error = error[:500]  # Ограничиваем длину
        job.updated_at = datetime.now(timezone.utc)
        
        if job.attempts >= job.max_attempts:
            job.state = SweepState.FAILED
            logger.error(
                f"[{job.chain}] Job {job.id} failed permanently after "
                f"{job.attempts} attempts: {error}"
            )
        else:
            # Экспоненциальный backoff
            delay_minutes = min(2 ** job.attempts, 60)
            job.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
            logger.warning(
                f"[{job.chain}] Job {job.id} attempt {job.attempts} failed, "
                f"retry in {delay_minutes} min"
            )


async def run_unified_sweeper(
    interval_sec: int = 30,
    max_jobs_per_chain: int = 20,
) -> None:
    """
    Запустить unified batch sweeper в бесконечном цикле.
    
    Args:
        interval_sec: Интервал между итерациями
        max_jobs_per_chain: Максимум jobs на сеть за итерацию
    """
    logger.info(f"Starting Unified Batch Sweeper (interval={interval_sec}s)")
    
    sweeper = UnifiedBatchSweeper()
    
    while True:
        try:
            results = await sweeper.process_pending_jobs(
                max_jobs_per_chain=max_jobs_per_chain,
            )
            
            # Логируем статистику
            total_completed = sum(r.completed for r in results.values())
            total_failed = sum(r.failed for r in results.values())
            
            if total_completed or total_failed:
                logger.info(
                    f"Sweep iteration: {total_completed} completed, "
                    f"{total_failed} failed across {len(results)} chains"
                )
                
        except Exception as e:
            logger.error(f"Sweeper iteration error: {e}")
        
        await asyncio.sleep(interval_sec)


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    asyncio.run(run_unified_sweeper())
