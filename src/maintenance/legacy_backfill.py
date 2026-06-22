"""Legacy persistent wallet backfill via configured non-RPC scanner."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import Deposit, UserWallet, WalletAddress
from src.services.user_wallet_service import UserWalletService
from src.workers.persistent_poller import _build_oklink_fetcher, _parse_transfer_log


@dataclass(frozen=True)
class LegacyBackfillResult:
    """Result for one legacy persistent address backfill range."""

    chain: str
    from_block: int
    to_block: int
    watched_address_count: int
    fetched_log_count: int
    candidate_deposit_count: int
    inserted_deposit_count: int
    skipped_existing_count: int
    rejected_deposit_count: int
    is_complete: bool
    failed_address_count: int
    execute: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def load_legacy_address_map(
    session: AsyncSession,
    chain: str,
    *,
    only_active: bool = True,
) -> dict[str, WalletAddress]:
    """Load legacy persistent wallet addresses for one chain."""
    conditions = [WalletAddress.chain == chain]
    if only_active:
        conditions.extend(
            [
                WalletAddress.is_active == True,  # noqa: E712
                UserWallet.is_active == True,  # noqa: E712
            ]
        )

    stmt = (
        select(WalletAddress)
        .join(UserWallet, WalletAddress.user_wallet_id == UserWallet.id)
        .options(selectinload(WalletAddress.user_wallet))
        .where(and_(*conditions))
    )
    result = await session.execute(stmt)
    return {address.address.lower(): address for address in result.scalars().all()}


async def backfill_legacy_chain(
    session: AsyncSession,
    chain: str,
    from_block: int,
    to_block: int,
    *,
    fetcher_override=None,
    execute: bool = False,
    only_active: bool = True,
) -> LegacyBackfillResult:
    """
    Re-scan legacy persistent addresses for a fixed block range.

    Does not advance poller checkpoints. Writes only when execute=True.
    """
    if from_block < 0 or to_block < from_block:
        raise ValueError("invalid backfill block range")

    from src.blockchain.chains import get_chain_config

    config = get_chain_config(chain)
    address_map = await load_legacy_address_map(
        session,
        chain,
        only_active=only_active,
    )
    if not address_map:
        return LegacyBackfillResult(
            chain=chain,
            from_block=from_block,
            to_block=to_block,
            watched_address_count=0,
            fetched_log_count=0,
            candidate_deposit_count=0,
            inserted_deposit_count=0,
            skipped_existing_count=0,
            rejected_deposit_count=0,
            is_complete=True,
            failed_address_count=0,
            execute=execute,
        )

    fetcher = fetcher_override or _build_oklink_fetcher(chain, config)
    should_close = fetcher_override is None
    try:
        fetch_result = await fetcher.fetch_transfer_logs(
            from_block=from_block,
            to_block=to_block,
            to_addresses=list(address_map.keys()),
            token_contracts=[token.contract_address for token in config.tokens.values()],
        )
    finally:
        if should_close and hasattr(fetcher, "aclose"):
            await fetcher.aclose()

    if not fetch_result.is_complete:
        return LegacyBackfillResult(
            chain=chain,
            from_block=from_block,
            to_block=to_block,
            watched_address_count=len(address_map),
            fetched_log_count=len(fetch_result.logs),
            candidate_deposit_count=0,
            inserted_deposit_count=0,
            skipped_existing_count=0,
            rejected_deposit_count=0,
            is_complete=False,
            failed_address_count=fetch_result.failed_address_count,
            execute=execute,
        )

    wallet_service = UserWalletService(session)
    candidate_count = 0
    inserted_count = 0
    skipped_existing_count = 0
    rejected_count = 0

    for log in fetch_result.logs:
        transfer = _parse_transfer_log(chain, log)
        if transfer is None:
            continue

        wallet_address = address_map.get(transfer.to_address.lower())
        if wallet_address is None:
            continue

        asset = _asset_for_token(config, transfer.token_contract)
        if asset is None:
            continue

        candidate_count += 1
        tx_hash = _normalize_tx_hash(transfer.tx_hash)
        existing = await _find_existing_deposit(session, chain, tx_hash, transfer.log_index)
        if existing is not None:
            skipped_existing_count += 1
            continue

        if not execute:
            continue

        deposit = await wallet_service.record_deposit(
            wallet_address=wallet_address,
            tx_hash=tx_hash,
            block_number=transfer.block_number,
            log_index=transfer.log_index,
            amount=transfer.amount,
            asset=asset,
            token_contract=transfer.token_contract,
            from_address=transfer.from_address,
            required_confirmations=config.confirmations,
        )
        if deposit is None:
            rejected_count += 1
        else:
            inserted_count += 1

    return LegacyBackfillResult(
        chain=chain,
        from_block=from_block,
        to_block=to_block,
        watched_address_count=len(address_map),
        fetched_log_count=len(fetch_result.logs),
        candidate_deposit_count=candidate_count,
        inserted_deposit_count=inserted_count,
        skipped_existing_count=skipped_existing_count,
        rejected_deposit_count=rejected_count,
        is_complete=True,
        failed_address_count=0,
        execute=execute,
    )


async def _find_existing_deposit(
    session: AsyncSession,
    chain: str,
    tx_hash: str,
    log_index: int,
) -> Deposit | None:
    stmt = select(Deposit).where(
        and_(
            Deposit.chain == chain,
            Deposit.tx_hash == tx_hash,
            Deposit.log_index == log_index,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _asset_for_token(config, token_contract: str) -> str | None:
    token_contract = token_contract.lower()
    for symbol, token in config.tokens.items():
        if token.contract_address.lower() == token_contract:
            return symbol
    return None


def _normalize_tx_hash(tx_hash: str) -> str:
    return tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"


def decimal_default(value):
    """JSON serializer for CLI output."""
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
