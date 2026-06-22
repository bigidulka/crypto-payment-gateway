"""Legacy persistent sweep drain reporting and job creation."""

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import (
    Deposit,
    DepositStatus,
    SweepSource,
    SweepState,
    UnifiedSweepJob,
)

SweepJobFactory = Callable[[AsyncSession, Deposit], Awaitable[UnifiedSweepJob | None]]


@dataclass(frozen=True)
class LegacySweepDrainReport:
    """DB-side persistent sweep drain state."""

    chain: str | None
    deposits_by_status: dict[str, dict[str, str | int]] = field(default_factory=dict)
    jobs_by_state: dict[str, dict[str, str | int]] = field(default_factory=dict)
    missing_job_count: int = 0
    missing_job_amount: str = "0"
    non_completed_job_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MissingSweepJobsResult:
    """Result of missing sweep job creation pass."""

    chain: str | None
    candidate_count: int
    created_count: int
    skipped_count: int
    execute: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def build_legacy_sweep_drain_report(
    session: AsyncSession,
    *,
    chain: str | None = None,
) -> LegacySweepDrainReport:
    """Build report for persistent deposits and sweep jobs."""
    deposit_conditions = []
    job_conditions = [UnifiedSweepJob.source == SweepSource.PERSISTENT]
    missing_conditions = [Deposit.status == DepositStatus.CONFIRMED]
    if chain:
        deposit_conditions.append(Deposit.chain == chain)
        job_conditions.append(UnifiedSweepJob.chain == chain)
        missing_conditions.append(Deposit.chain == chain)

    deposits_by_status = await _group_deposits_by_status(session, deposit_conditions)
    jobs_by_state = await _group_jobs_by_state(session, job_conditions)
    missing_job_count, missing_job_amount = await _count_missing_jobs(
        session,
        missing_conditions,
    )
    non_completed_job_count = await _count_non_completed_jobs(session, job_conditions)

    return LegacySweepDrainReport(
        chain=chain,
        deposits_by_status=deposits_by_status,
        jobs_by_state=jobs_by_state,
        missing_job_count=missing_job_count,
        missing_job_amount=str(missing_job_amount),
        non_completed_job_count=non_completed_job_count,
    )


async def create_missing_persistent_sweep_jobs(
    session: AsyncSession,
    *,
    chain: str | None = None,
    execute: bool = False,
    limit: int | None = None,
    sweep_job_factory: SweepJobFactory | None = None,
) -> MissingSweepJobsResult:
    """Create missing UnifiedSweepJob rows for confirmed legacy deposits."""
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")

    stmt = (
        select(Deposit)
        .options(
            selectinload(Deposit.wallet_address),
            selectinload(Deposit.user_wallet),
        )
        .where(Deposit.status == DepositStatus.CONFIRMED)
        .where(~Deposit.id.in_(select(UnifiedSweepJob.source_id).where(
            UnifiedSweepJob.source == SweepSource.PERSISTENT
        )))
        .order_by(Deposit.confirmed_at.asc(), Deposit.detected_at.asc())
    )
    if chain:
        stmt = stmt.where(Deposit.chain == chain)
    if limit is not None:
        stmt = stmt.limit(limit)

    result = await session.execute(stmt)
    deposits = list(result.scalars().all())

    if not execute:
        return MissingSweepJobsResult(
            chain=chain,
            candidate_count=len(deposits),
            created_count=0,
            skipped_count=0,
            execute=False,
        )

    if sweep_job_factory is None:
        from src.workers.persistent_poller import create_unified_sweep_job

        sweep_job_factory = create_unified_sweep_job

    created_count = 0
    skipped_count = 0
    for deposit in deposits:
        job = await sweep_job_factory(session, deposit)
        if job is None:
            skipped_count += 1
        else:
            created_count += 1

    await session.commit()
    return MissingSweepJobsResult(
        chain=chain,
        candidate_count=len(deposits),
        created_count=created_count,
        skipped_count=skipped_count,
        execute=True,
    )


async def _group_deposits_by_status(
    session: AsyncSession,
    conditions: list,
) -> dict[str, dict[str, str | int]]:
    stmt = select(
        Deposit.status,
        func.count(Deposit.id),
        func.coalesce(func.sum(Deposit.amount), 0),
    ).group_by(Deposit.status)
    if conditions:
        stmt = stmt.where(and_(*conditions))

    rows = (await session.execute(stmt)).all()
    return {
        _enum_value(status): {"count": count, "amount": str(amount)}
        for status, count, amount in rows
    }


async def _group_jobs_by_state(
    session: AsyncSession,
    conditions: list,
) -> dict[str, dict[str, str | int]]:
    stmt = select(
        UnifiedSweepJob.state,
        func.count(UnifiedSweepJob.id),
        func.coalesce(func.sum(UnifiedSweepJob.amount), 0),
    ).group_by(UnifiedSweepJob.state)
    if conditions:
        stmt = stmt.where(and_(*conditions))

    rows = (await session.execute(stmt)).all()
    return {
        _enum_value(state): {"count": count, "amount": str(amount)}
        for state, count, amount in rows
    }


async def _count_missing_jobs(
    session: AsyncSession,
    conditions: list,
) -> tuple[int, Decimal]:
    stmt = select(
        func.count(Deposit.id),
        func.coalesce(func.sum(Deposit.amount), 0),
    ).where(
        ~Deposit.id.in_(
            select(UnifiedSweepJob.source_id).where(
                UnifiedSweepJob.source == SweepSource.PERSISTENT
            )
        )
    )
    if conditions:
        stmt = stmt.where(and_(*conditions))

    count, amount = (await session.execute(stmt)).one()
    return int(count or 0), Decimal(str(amount or 0))


async def _count_non_completed_jobs(
    session: AsyncSession,
    conditions: list,
) -> int:
    stmt = select(func.count(UnifiedSweepJob.id)).where(
        UnifiedSweepJob.state != SweepState.COMPLETED
    )
    if conditions:
        stmt = stmt.where(and_(*conditions))

    return int((await session.execute(stmt)).scalar_one() or 0)


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)
