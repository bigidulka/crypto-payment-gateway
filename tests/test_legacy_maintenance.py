import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.blockchain.chains import get_chain_config
from src.core.deposit_security import ValidationResult
from src.db.models import (
    Base,
    Deposit,
    DepositStatus,
    Merchant,
    SweepSource,
    SweepState,
    UnifiedSweepJob,
    UserWallet,
    WalletAddress,
)
from src.maintenance.legacy_backfill import backfill_legacy_chain
from src.maintenance.legacy_sweep_drain import (
    build_legacy_sweep_drain_report,
    create_missing_persistent_sweep_jobs,
)


@dataclass(frozen=True)
class _FetchResult:
    logs: list[dict]
    is_complete: bool
    failed_address_count: int


class _Fetcher:
    def __init__(self, result: _FetchResult):
        self.result = result

    async def fetch_transfer_logs(self, **kwargs):
        return self.result


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _create_legacy_wallet(db_session, *, chain: str = "bsc") -> WalletAddress:
    merchant = Merchant(
        id=uuid.uuid4(),
        name="merchant",
        email=f"merchant-{uuid.uuid4().hex}@example.com",
    )
    wallet = UserWallet(
        id=uuid.uuid4(),
        merchant_id=merchant.id,
        external_user_id=f"user-{uuid.uuid4().hex}",
        is_active=True,
    )
    address = WalletAddress(
        id=uuid.uuid4(),
        user_wallet_id=wallet.id,
        chain=chain,
        address="0x" + "a" * 40,
        derivation_index=1,
        encrypted_private_key="encrypted",
        is_active=True,
        last_scanned_block=100,
    )
    db_session.add_all([merchant, wallet, address])
    await db_session.commit()
    return address


def _transfer_log(chain: str = "bsc") -> dict:
    token = get_chain_config(chain).tokens["USDT"]
    from_address = "0x" + "1" * 40
    to_address = "0x" + "a" * 40
    return {
        "transactionHash": "0x" + "2" * 64,
        "logIndex": "0x0",
        "blockNumber": "0x78",
        "address": token.contract_address,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + "0" * 24 + from_address[2:],
            "0x" + "0" * 24 + to_address[2:],
        ],
        "data": hex(10**18),
    }


@pytest.mark.asyncio
async def test_legacy_backfill_inserts_once_idempotently(db_session, monkeypatch):
    await _create_legacy_wallet(db_session)
    monkeypatch.setattr(
        "src.services.user_wallet_service.validate_deposit",
        lambda **kwargs: ValidationResult(True, None),
    )
    fetcher = _Fetcher(_FetchResult([_transfer_log()], True, 0))

    first = await backfill_legacy_chain(
        db_session,
        "bsc",
        100,
        150,
        fetcher_override=fetcher,
        execute=True,
    )
    second = await backfill_legacy_chain(
        db_session,
        "bsc",
        100,
        150,
        fetcher_override=fetcher,
        execute=True,
    )

    deposits = (await db_session.execute(select(Deposit))).scalars().all()
    assert first.inserted_deposit_count == 1
    assert first.candidate_deposit_count == 1
    assert second.inserted_deposit_count == 0
    assert second.skipped_existing_count == 1
    assert len(deposits) == 1
    assert deposits[0].tx_hash == "0x" + "2" * 64


@pytest.mark.asyncio
async def test_legacy_backfill_incomplete_scan_writes_nothing(db_session):
    await _create_legacy_wallet(db_session)
    fetcher = _Fetcher(_FetchResult([_transfer_log()], False, 1))

    result = await backfill_legacy_chain(
        db_session,
        "bsc",
        100,
        150,
        fetcher_override=fetcher,
        execute=True,
    )

    deposits = (await db_session.execute(select(Deposit))).scalars().all()
    assert result.is_complete is False
    assert result.failed_address_count == 1
    assert result.inserted_deposit_count == 0
    assert deposits == []


@pytest.mark.asyncio
async def test_legacy_sweep_drain_report_and_create_missing_jobs(db_session):
    address = await _create_legacy_wallet(db_session)
    deposit = Deposit(
        id=uuid.uuid4(),
        user_wallet_id=address.user_wallet_id,
        wallet_address_id=address.id,
        chain=address.chain,
        tx_hash="0x" + "3" * 64,
        block_number=120,
        log_index=0,
        amount=Decimal("2"),
        asset="USDT",
        token_contract=get_chain_config(address.chain).tokens["USDT"].contract_address,
        from_address="0x" + "4" * 40,
        status=DepositStatus.CONFIRMED,
        confirmations=12,
        required_confirmations=12,
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
        confirmed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(deposit)
    await db_session.commit()

    report = await build_legacy_sweep_drain_report(db_session, chain=address.chain)
    dry_run = await create_missing_persistent_sweep_jobs(
        db_session,
        chain=address.chain,
        execute=False,
    )

    async def _factory(session, dep):
        job = UnifiedSweepJob(
            id=uuid.uuid4(),
            source=SweepSource.PERSISTENT,
            source_id=dep.id,
            chain=dep.chain,
            token=dep.asset,
            token_contract=dep.token_contract,
            from_address=address.address,
            to_address="0x" + "f" * 40,
            encrypted_private_key=address.encrypted_private_key,
            amount=dep.amount,
            amount_raw=str(2 * 10**18),
            state=SweepState.PENDING_GAS,
        )
        session.add(job)
        return job

    created = await create_missing_persistent_sweep_jobs(
        db_session,
        chain=address.chain,
        execute=True,
        sweep_job_factory=_factory,
    )
    after = await build_legacy_sweep_drain_report(db_session, chain=address.chain)

    assert report.missing_job_count == 1
    assert Decimal(report.missing_job_amount) == Decimal("2")
    assert dry_run.candidate_count == 1
    assert dry_run.created_count == 0
    assert created.created_count == 1
    assert after.missing_job_count == 0
    assert after.jobs_by_state[SweepState.PENDING_GAS.value]["count"] == 1
