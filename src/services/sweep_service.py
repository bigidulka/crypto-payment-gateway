"""
Sweep Service - сервис для создания и управления sweep jobs.

Все сервисы используют этот сервис для создания UnifiedSweepJob.
"""

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.db.models import (
    UnifiedSweepJob,
    SweepSource,
    SweepState,
    PaymentSession,
    DepositAddress,
    Deposit,
    WalletAddress,
)
from src.blockchain.chains import get_chain_config, get_token_contract

logger = logging.getLogger(__name__)


class SweepService:
    """Сервис для управления sweep jobs."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    async def create_invoice_sweep_job(
        self,
        payment_session: PaymentSession,
        deposit_address: DepositAddress,
        amount: Decimal,
        amount_raw: int,
    ) -> UnifiedSweepJob:
        """
        Создать sweep job для оплаченного инвойса.
        
        Args:
            payment_session: Сессия оплаты
            deposit_address: Deposit адрес с приватным ключом
            amount: Сумма токенов
            amount_raw: Сумма в raw units (без decimals)
            
        Returns:
            Созданный UnifiedSweepJob
        """
        chain = payment_session.chain
        token = payment_session.token
        
        # Получаем treasury адрес
        treasury = self.settings.get_treasury_address(chain)
        
        # Получаем контракт токена
        token_contract = get_token_contract(chain, token)
        
        job = UnifiedSweepJob(
            source=SweepSource.INVOICE,
            source_id=payment_session.id,
            chain=chain,
            token=token,
            token_contract=token_contract,
            from_address=deposit_address.address,
            to_address=treasury,
            encrypted_private_key=deposit_address.encrypted_privkey,
            amount=amount,
            amount_raw=str(amount_raw),
            state=SweepState.PENDING_GAS,
            priority=self._calculate_priority(amount),
        )
        
        self.session.add(job)
        await self.session.flush()
        
        logger.info(
            f"Created invoice sweep job {job.id}: "
            f"{amount} {token} on {chain} from {deposit_address.address[:10]}..."
        )
        
        return job

    async def create_persistent_sweep_job(
        self,
        deposit: Deposit,
        wallet_address: WalletAddress,
    ) -> UnifiedSweepJob:
        """
        Создать sweep job для persistent deposit.
        
        Args:
            deposit: Подтверждённый депозит
            wallet_address: Wallet адрес с приватным ключом
            
        Returns:
            Созданный UnifiedSweepJob
        """
        chain = deposit.chain
        token = deposit.asset
        
        # Получаем treasury адрес
        treasury = self.settings.get_treasury_address(chain)
        
        # Получаем контракт токена
        token_contract = deposit.token_contract
        
        # Конвертируем amount в raw
        config = get_chain_config(chain)
        token_config = config.tokens.get(token)
        decimals = token_config.decimals if token_config else 18
        amount_raw = int(deposit.amount * (10 ** decimals))
        
        job = UnifiedSweepJob(
            source=SweepSource.PERSISTENT,
            source_id=deposit.id,
            chain=chain,
            token=token,
            token_contract=token_contract,
            from_address=wallet_address.address,
            to_address=treasury,
            encrypted_private_key=wallet_address.encrypted_private_key,
            amount=deposit.amount,
            amount_raw=str(amount_raw),
            state=SweepState.PENDING_GAS,
            priority=self._calculate_priority(deposit.amount),
        )
        
        self.session.add(job)
        await self.session.flush()
        
        logger.info(
            f"Created persistent sweep job {job.id}: "
            f"{deposit.amount} {token} on {chain} from {wallet_address.address[:10]}..."
        )
        
        return job

    async def get_pending_jobs(
        self,
        chain: Optional[str] = None,
        limit: int = 100,
    ) -> list[UnifiedSweepJob]:
        """
        Получить pending sweep jobs для обработки.
        
        Args:
            chain: Фильтр по сети (None = все)
            limit: Максимум jobs
            
        Returns:
            Список jobs отсортированных по приоритету
        """
        now = datetime.now(timezone.utc)
        
        conditions = [
            UnifiedSweepJob.state.in_([
                SweepState.PENDING_GAS,
                SweepState.FUNDING,
                SweepState.SWEEPING,
            ]),
            # Либо next_retry_at не установлен, либо уже прошёл
            (UnifiedSweepJob.next_retry_at.is_(None)) | 
            (UnifiedSweepJob.next_retry_at <= now),
        ]
        
        if chain:
            conditions.append(UnifiedSweepJob.chain == chain)
        
        stmt = (
            select(UnifiedSweepJob)
            .where(and_(*conditions))
            .order_by(
                UnifiedSweepJob.priority.desc(),
                UnifiedSweepJob.created_at.asc(),
            )
            .limit(limit)
        )
        
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_pending_jobs_by_chain(
        self,
        limit_per_chain: int = 50,
    ) -> dict[str, list[UnifiedSweepJob]]:
        """
        Получить pending jobs сгруппированные по сетям.
        
        Returns:
            Dict[chain -> list[jobs]]
        """
        jobs = await self.get_pending_jobs(limit=limit_per_chain * 10)
        
        by_chain: dict[str, list[UnifiedSweepJob]] = {}
        for job in jobs:
            if job.chain not in by_chain:
                by_chain[job.chain] = []
            if len(by_chain[job.chain]) < limit_per_chain:
                by_chain[job.chain].append(job)
        
        return by_chain

    async def job_exists(
        self,
        source: SweepSource,
        source_id: uuid.UUID,
    ) -> bool:
        """Проверить существует ли уже job для данного источника."""
        stmt = select(UnifiedSweepJob.id).where(
            and_(
                UnifiedSweepJob.source == source,
                UnifiedSweepJob.source_id == source_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def mark_funding(
        self,
        job: UnifiedSweepJob,
        gas_tx_hash: str,
    ) -> None:
        """Отметить что газ отправлен."""
        job.state = SweepState.FUNDING
        job.gas_tx_hash = gas_tx_hash
        job.updated_at = datetime.now(timezone.utc)

    async def mark_sweeping(
        self,
        job: UnifiedSweepJob,
        sweep_tx_hash: str,
    ) -> None:
        """Отметить что sweep транзакция отправлена."""
        job.state = SweepState.SWEEPING
        job.sweep_tx_hash = sweep_tx_hash
        job.updated_at = datetime.now(timezone.utc)

    async def mark_completed(
        self,
        job: UnifiedSweepJob,
        sweep_tx_hash: Optional[str] = None,
    ) -> None:
        """Отметить job как завершённый."""
        job.state = SweepState.COMPLETED
        if sweep_tx_hash:
            job.sweep_tx_hash = sweep_tx_hash
        job.completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        
        logger.info(f"Sweep job {job.id} completed: {job.amount} {job.token} on {job.chain}")

    async def mark_failed(
        self,
        job: UnifiedSweepJob,
        error: str,
    ) -> None:
        """Отметить job как проваленный."""
        job.attempts += 1
        job.last_error = error
        job.updated_at = datetime.now(timezone.utc)
        
        if job.attempts >= job.max_attempts:
            job.state = SweepState.FAILED
            logger.error(f"Sweep job {job.id} failed after {job.attempts} attempts: {error}")
        else:
            # Экспоненциальный backoff: 1, 2, 4, 8, 16... минут
            delay_minutes = min(2 ** job.attempts, 60)
            job.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
            logger.warning(
                f"Sweep job {job.id} attempt {job.attempts} failed, "
                f"retry in {delay_minutes} min: {error}"
            )

    def _calculate_priority(self, amount: Decimal) -> int:
        """
        Рассчитать приоритет на основе суммы.
        Больше сумма = выше приоритет.
        """
        import math
        return max(0, int(math.log10(float(amount))))
