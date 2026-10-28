"""Per-address durable scan cursor; crash-safe reconciliation and gas/sweep operations."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.blockchain.chains import ChainType, get_chain_config
from src.blockchain.prepared import ChainEvidenceError, ChainRPCError
from src.core.config import get_settings
from src.crypto.encryption import decrypt_private_key, decrypt_secret
from src.db.models import (
    Invoice,
    OnchainOperation,
    OnchainScanCursor,
    PaymentSession,
    SweepState,
    UnifiedSweepJob,
)
from src.db.session import close_db, get_session_factory, require_runtime_database_ready
from src.ledger.amounts import decimal_to_atomic
from src.services.gateway_provider_service import GatewayError
from src.services.onchain_gateway_service import ACTIVE, OnchainGatewayService, adapter_for, digest

logger = logging.getLogger(__name__)


def enabled_merchants():
    return [UUID(value) for value in get_settings().ledger_rail_orchestration_merchant_ids]


async def job_operation(session, merchant_id, job, prefix):
    rows = (
        await session.scalars(
            select(OnchainOperation)
            .where(
                OnchainOperation.merchant_id == merchant_id,
                (OnchainOperation.key == f"{prefix}:{job.id}")
                | OnchainOperation.key.like(f"{prefix}:{job.id}:%"),
            )
            .order_by(OnchainOperation.created_at.desc())
        )
    ).all()
    latest = rows[0] if rows else None
    if (
        latest
        and latest.status == "failed"
        and job.state == SweepState.PENDING_GAS
        and job.last_error is None
    ):
        latest = None
    key = f"{prefix}:{job.id}" + (f":{len(rows)}" if rows else "")
    return latest, key


async def scan_once(factory):
    settings = get_settings()
    async with factory() as session:
        cursors = (
            await session.scalars(
                select(OnchainScanCursor)
                .join(PaymentSession, PaymentSession.id == OnchainScanCursor.session_id)
                .join(Invoice, Invoice.id == PaymentSession.invoice_id)
                .where(Invoice.merchant_id.in_(enabled_merchants()))
                .order_by(OnchainScanCursor.updated_at)
                .limit(settings.gateway_provider_poll_batch_size)
            )
        ).all()
        ids = [c.session_id for c in cursors]
    for session_id in ids:
        try:
            async with factory() as session, session.begin():
                cursor = await session.scalar(
                    select(OnchainScanCursor)
                    .where(OnchainScanCursor.session_id == session_id)
                    .with_for_update(skip_locked=True)
                )
                if cursor is None:
                    continue
                payment = await session.scalar(
                    select(PaymentSession)
                    .where(PaymentSession.id == session_id)
                    .options(selectinload(PaymentSession.deposit_address))
                )
                config = get_chain_config(cursor.chain)
                adapter = adapter_for(cursor.chain)
                try:
                    head = await adapter.get_latest_block()
                    upper = (
                        head
                        if config.chain_type == ChainType.SOLANA
                        else head - max(config.confirmations, config.reorg_buffer) + 1
                    )
                    upper = min(upper, cursor.last_block + config.scan_window)
                    if upper > cursor.last_block:
                        events = await adapter.get_transfer_events(
                            cursor.last_block + 1, upper, [payment.deposit_address.address]
                        )
                        service = OnchainGatewayService(session)
                        for event in events:
                            await service.consume_event(event, session_id=session_id)
                        cursor.last_block = upper
                    cursor.updated_at = datetime.now(UTC)
                finally:
                    await adapter.close()
        except (ChainRPCError, ChainEvidenceError, GatewayError):
            # Roll back every fact in this window, retain its checkpoint and give
            # the other addresses a turn even when one RPC/trace is unavailable.
            async with factory() as session, session.begin():
                cursor = await session.get(OnchainScanCursor, session_id, with_for_update=True)
                cursor.updated_at = datetime.now(UTC)
            logger.warning("Scan window unavailable; checkpoint retained")


async def reconcile_once(factory):
    async with factory() as session:
        rows = (
            await session.scalars(
                select(OnchainOperation)
                .where(
                    OnchainOperation.status.in_(ACTIVE),
                    OnchainOperation.merchant_id.in_(enabled_merchants()),
                    (OnchainOperation.tx_id.is_not(None))
                    | (
                        (OnchainOperation.purpose == "payout")
                        & get_settings().onchain_payout_enabled
                    ),
                )
                .order_by(OnchainOperation.updated_at)
                .limit(get_settings().gateway_provider_poll_batch_size)
            )
        ).all()
        targets = [(r.id, r.merchant_id) for r in rows]
    for operation_id, merchant_id in targets:
        async with factory() as session:
            service = OnchainGatewayService(session)
            operation = await session.get(OnchainOperation, operation_id)
            if operation.status == "preparing" and not operation.tx_id:
                private = getattr(
                    service.settings,
                    f"{operation.chain}_treasury_private_key",
                    service.settings.evm_treasury_private_key,
                ).get_secret_value()
                adapter = adapter_for(operation.chain)
                try:
                    await service.submit(operation_id, private, adapter)
                finally:
                    await adapter.close()
            await service.reconcile(merchant_id, operation_id)


async def sweep_once(factory):
    settings = get_settings()
    # New durable sweeper owns non-EVM jobs and jobs with a verified onchain receipt.
    async with factory() as session:
        jobs = (
            await session.scalars(
                select(UnifiedSweepJob)
                .join(PaymentSession, PaymentSession.id == UnifiedSweepJob.source_id)
                .join(OnchainScanCursor, OnchainScanCursor.session_id == PaymentSession.id)
                .join(Invoice, Invoice.id == PaymentSession.invoice_id)
                .where(
                    Invoice.merchant_id.in_(enabled_merchants()),
                    UnifiedSweepJob.state.in_(
                        (SweepState.PENDING_GAS, SweepState.FUNDING, SweepState.SWEEPING)
                    ),
                )
                .order_by(UnifiedSweepJob.updated_at)
                .limit(settings.gateway_provider_poll_batch_size)
            )
        ).all()
        ids = [j.id for j in jobs]
    for job_id in ids:
        async with factory() as session:
            job = await session.scalar(
                select(UnifiedSweepJob)
                .where(UnifiedSweepJob.id == job_id)
                .with_for_update(skip_locked=True)
            )
            if not job:
                continue
            payment = await session.get(PaymentSession, job.source_id)
            invoice = await session.get(Invoice, payment.invoice_id)
            service = OnchainGatewayService(session)
            config = get_chain_config(job.chain)
            adapter = adapter_for(job.chain)
            try:
                job.updated_at = datetime.now(UTC)
                sender = adapter.normalize_address(job.from_address)
                treasury = adapter.normalize_address(job.to_address)
                decryptor = (
                    decrypt_private_key if config.chain_type == ChainType.EVM else decrypt_secret
                )
                private = decryptor(
                    bytes.fromhex(job.encrypted_private_key),
                    settings.encryption_key.get_secret_value(),
                )
                native = config.is_native_asset(job.token)
                token = None if native else job.token_contract
                operation, sweep_key = await job_operation(
                    session, invoice.merchant_id, job, "sweep"
                )
                if operation:
                    if operation.status == "preparing" and not operation.tx_id:
                        await service.submit(operation.id, private, adapter)
                    job.sweep_tx_hash = operation.tx_id
                    if operation.status == "completed":
                        job.state, job.completed_at = SweepState.COMPLETED, datetime.now(UTC)
                    elif operation.status == "failed":
                        job.state, job.last_error = SweepState.FAILED, operation.last_error
                    else:
                        job.state = SweepState.SWEEPING
                    await session.commit()
                    continue
                balance = decimal_to_atomic(
                    await adapter.get_native_balance(sender), config.native_decimals
                )
                if config.chain_type == ChainType.TON:
                    gas = decimal_to_atomic(Decimal(settings.ton_deposit_gas_budget), 9)
                elif config.chain_type == ChainType.SOLANA:
                    fee = await adapter.estimate_transfer_fee(sender, treasury, token, job.amount)
                    gas = fee.estimated_fee_raw
                    if token:
                        gas += await adapter.native_rent_reserve()
                else:
                    # EVM raw gas estimation requires native coin on some nodes; use the
                    # configured per-invoice cap for a one-time gas topup, then exact RPC fee.
                    gas = config.max_gas_cost_wei
                needed = gas + (int(job.amount_raw) if native else 0)
                funding, fund_key = await job_operation(session, invoice.merchant_id, job, "fund")
                if balance < needed:
                    if funding:
                        if funding.status == "preparing" and not funding.tx_id:
                            funder_key = getattr(
                                settings,
                                f"{job.chain}_funder_private_key",
                                settings.funder_private_key,
                            ).get_secret_value()
                            await service.submit(funding.id, funder_key, adapter)
                        job.gas_tx_hash = funding.tx_id
                        job.state = (
                            SweepState.FUNDING if funding.status in ACTIVE else SweepState.FAILED
                        )
                        job.last_error = (
                            "funding_unresolved"
                            if funding.status in ACTIVE
                            else "funding_budget_insufficient"
                        )
                        await session.commit()
                        continue
                    funder_key = getattr(
                        settings, f"{job.chain}_funder_private_key", settings.funder_private_key
                    ).get_secret_value()
                    if not funder_key:
                        job.last_error = "separate_gas_funder_required"
                        await session.commit()
                        continue
                    if config.chain_type == ChainType.SOLANA:
                        from solders.keypair import Keypair

                        funder_address = str(Keypair.from_base58_string(funder_key).pubkey())
                    elif config.chain_type == ChainType.TON:
                        from src.blockchain.ton_keys import wallet_from_seed

                        funder_address = wallet_from_seed(
                            funder_key, settings.ton_wallet_version, settings.ton_workchain
                        ).address.to_string(False)
                    else:
                        from eth_account import Account

                        funder_address = Account.from_key(funder_key).address
                    funder_address = adapter.normalize_address(funder_address)
                    if funder_address in (treasury, sender):
                        job.last_error = "gas_funder_must_be_separate"
                        await session.commit()
                        continue
                    await service._wallet_lock(job.chain, funder_address)
                    funding = OnchainOperation(
                        merchant_id=invoice.merchant_id,
                        purpose="funding",
                        key=fund_key,
                        digest=digest([str(job.id), sender, str(needed - balance)]),
                        chain=job.chain,
                        sender=funder_address,
                        destination=sender,
                        token="",
                        amount_atomic=needed - balance,
                        status="preparing",
                    )
                    session.add(funding)
                    job.state = SweepState.FUNDING
                    await session.commit()
                    await service.submit(funding.id, funder_key, adapter)
                    job.gas_tx_hash = funding.tx_id
                    await session.commit()
                    continue
                operation = OnchainOperation(
                    merchant_id=invoice.merchant_id,
                    purpose="sweep",
                    key=sweep_key,
                    digest=digest([str(job.id), treasury, job.amount_raw]),
                    chain=job.chain,
                    sender=sender,
                    destination=treasury,
                    token=token or "",
                    amount_atomic=int(job.amount_raw),
                    status="preparing",
                )
                session.add(operation)
                job.state = SweepState.SWEEPING
                await session.commit()
                await service.submit(operation.id, private, adapter)
                job.sweep_tx_hash = operation.tx_id
                await session.commit()
            finally:
                await adapter.close()


async def run():
    settings = get_settings()
    if not settings.onchain_gateway_enabled or not settings.ledger_rail_orchestration_enabled:
        logger.info("On-chain gateway is disabled")
        return
    await require_runtime_database_ready()
    factory = get_session_factory()
    try:
        while True:
            for stage in (scan_once, reconcile_once, sweep_once):
                try:
                    await stage(factory)
                except (ChainRPCError, ChainEvidenceError):
                    logger.warning("Chain evidence unavailable; cursor and reservations retained")
                except Exception:
                    logger.error("On-chain pass failed; cursor and reservations retained")
            await asyncio.sleep(settings.non_evm_poll_interval_seconds)
    finally:
        await close_db()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
