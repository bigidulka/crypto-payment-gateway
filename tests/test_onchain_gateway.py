"""Isolated PostgreSQL chain receipt, reserve/replay and tenant boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from solders.keypair import Keypair
from sqlalchemy import func, select

from src.blockchain.base_adapter import ChainType, TransferEvent
from src.blockchain.prepared import ChainRPCError, PreparedTransfer, TransferReceipt
from src.core.config import get_settings
from src.crypto.encryption import encrypt_secret
from src.db.models import (
    DepositAddress,
    InvoiceStatus,
    LedgerEntry,
    OnchainOperation,
    OnchainReceipt,
    OnchainScanCursor,
    PaymentSession,
    SweepState,
    UnifiedSweepJob,
)
from src.services.gateway_provider_service import GatewayError
from src.services.invoice_service import InvoiceService
from src.services.onchain_gateway_service import OnchainGatewayService

pytest_plugins = ("tests.test_gateway_orchestration",)


async def setup_chain(gateway, monkeypatch, *, symbol="USDC", nominal="100"):
    sender = str(Keypair.from_seed(uuid4().bytes + uuid4().bytes).pubkey())
    treasury = str(Keypair.from_seed(uuid4().bytes + uuid4().bytes).pubkey())
    monkeypatch.setenv("NON_EVM_ENABLED", "true")
    monkeypatch.setenv("ONCHAIN_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("ONCHAIN_PAYOUT_ENABLED", "true")
    monkeypatch.setenv("SOLANA_TREASURY_ADDRESS", treasury)
    monkeypatch.setenv("SOLANA_TREASURY_PRIVATE_KEY", "isolated-test-key")
    get_settings.cache_clear()
    async with gateway.factory() as session:
        invoice = await InvoiceService(session).create_invoice(
            gateway.merchant, Decimal(nominal), symbol, ["solana"], idempotency_key=str(uuid4())
        )
        address = DepositAddress(
            address=sender,
            encrypted_privkey=encrypt_secret(
                "unit-key", get_settings().encryption_key.get_secret_value()
            ),
            chain_group="solana",
            derivation_path="m/44'/501'/123'/0'",
            derivation_index=uuid4().int % 2**31,
        )
        session.add(address)
        await session.flush()
        payment = PaymentSession(
            invoice_id=invoice.id,
            chain="solana",
            token=symbol,
            deposit_address_id=address.id,
            expires_at=invoice.expires_at,
        )
        session.add(payment)
        await session.commit()
        return invoice, sender, treasury


def transfer(sender, amount, tx_id, *, symbol="USDC"):
    from src.blockchain.chains import get_chain_config

    config = get_chain_config("solana")
    mint = "" if symbol == "SOL" else config.tokens[symbol].contract_address
    raw = int(Decimal(amount) * 10 ** config.get_asset_decimals(symbol))
    import hashlib

    from solders.signature import Signature

    tx_id = str(Signature.from_bytes(hashlib.sha512((tx_id + sender).encode()).digest()))
    return TransferEvent(
        tx_id,
        20,
        0,
        str(Keypair.from_seed(bytes([9]) * 32).pubkey()),
        sender,
        mint,
        symbol,
        Decimal(amount),
        raw,
        "solana",
        ChainType.SOLANA,
        int(datetime.now(UTC).timestamp()),
    )


async def test_topup_actual_receipt_and_replay(gateway, monkeypatch):
    invoice, sender, treasury = await setup_chain(gateway, monkeypatch)
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(transfer(sender, "60", "first"))
    async with gateway.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OnchainReceipt)
                .where(OnchainReceipt.invoice_id == invoice.id)
            )
            == 0
        )
        assert (await session.get(type(invoice), invoice.id)).status != InvoiceStatus.CONFIRMED
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(transfer(sender, "45", "second"))
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(transfer(sender, "45", "second"))
    async with gateway.factory() as session:
        receipt = await session.scalar(
            select(OnchainReceipt).where(OnchainReceipt.invoice_id == invoice.id)
        )
        assert int(receipt.amount_atomic) == 105000000
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.ledger_transaction_id == receipt.transaction_id)
            )
            == 2
        )
    proof = await gateway.service.receipt(gateway.merchant.id, invoice.id)
    assert proof["receipt"]["gross"] == proof["receipt"]["net"] == Decimal("105")
    with pytest.raises(GatewayError):
        await gateway.service.receipt(gateway.merchants[1].id, invoice.id)


async def test_payout_unknown_reserves_restart_receipt_releases_once(gateway, monkeypatch):
    invoice, sender, treasury = await setup_chain(gateway, monkeypatch)
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(transfer(sender, "100", "payout-source"))
    target = str(Keypair.from_seed(bytes([10]) * 32).pubkey())
    adapter = AsyncMock()
    adapter.normalize_address = lambda x: x
    adapter.get_token_balance.return_value = Decimal("1000000")
    adapter.get_native_balance.return_value = Decimal("1000000")
    mint = transfer(sender, "100", "x").token_address
    signature = str(uuid4())
    adapter.prepare_transfer.return_value = PreparedTransfer(
        "solana", signature, "signed-base64", treasury, target, mint, 30000000, 5000, {}
    )
    adapter.broadcast_prepared.return_value = TransferReceipt(
        "unknown", signature, reason="broadcast_outcome_unknown"
    )
    monkeypatch.setattr("src.services.onchain_gateway_service.adapter_for", lambda _: adapter)
    async with gateway.factory() as session:
        result = await OnchainGatewayService(session).create_payout(
            gateway.merchant.id, "solana", "USDC", Decimal("30"), target, "persistent-payout"
        )
    assert result["status"] == "unknown"
    async with gateway.factory() as session:
        repeated = await OnchainGatewayService(session).create_payout(
            gateway.merchant.id, "solana", "USDC", Decimal("30"), target, "persistent-payout"
        )
        assert repeated["id"] == result["id"]
        assert (
            await OnchainGatewayService(session).balance(
                gateway.merchant.id, "solana", "USDC", adapter
            )
        )["available"] == "70.000000"
    assert adapter.broadcast_prepared.call_count == 1
    # A lost response must be recoverable after pausing the chain and rotating treasury.
    monkeypatch.setenv("NON_EVM_ENABLED", "false")
    monkeypatch.setenv("ONCHAIN_GATEWAY_ENABLED", "false")
    monkeypatch.setenv("ONCHAIN_PAYOUT_ENABLED", "false")
    monkeypatch.setenv("SOLANA_TREASURY_ADDRESS", str(Keypair().pubkey()))
    get_settings.cache_clear()
    from uuid import UUID

    async with gateway.factory() as session:
        restored = await OnchainGatewayService(session).create_payout(
            gateway.merchant.id, "solana", "USDC", Decimal("30"), target, "persistent-payout"
        )
        assert restored["id"] == result["id"] and restored["status"] == "unknown"
        saved = await session.get(OnchainOperation, UUID(result["id"]))
        saved.last_broadcast_at = datetime.now(UTC) - timedelta(minutes=10)
        await session.commit()
        adapter.transfer_receipt.return_value = TransferReceipt("unknown", signature)
        await OnchainGatewayService(session).reconcile(gateway.merchant.id, saved.id, adapter)
        assert adapter.broadcast_prepared.call_count == 1
        with pytest.raises(GatewayError, match="different request"):
            await OnchainGatewayService(session).create_payout(
                gateway.merchant.id, "solana", "USDC", Decimal("31"), target, "persistent-payout"
            )
    adapter.transfer_receipt.return_value = TransferReceipt("completed", signature, 5000)
    from uuid import UUID

    async with gateway.factory() as session:
        await OnchainGatewayService(session).reconcile(
            gateway.merchant.id, UUID(result["id"]), adapter
        )
    async with gateway.factory() as session:
        completed = await OnchainGatewayService(session).reconcile(
            gateway.merchant.id, UUID(result["id"]), adapter
        )
        operation = await session.get(OnchainOperation, UUID(result["id"]))
        assert completed["status"] == "completed"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.ledger_transaction_id == operation.transaction_id)
            )
            == 2
        )
    assert adapter.transfer_receipt.call_count == 2


async def test_preparing_payout_is_resumed_after_crash_without_duplicate_broadcast(
    gateway, monkeypatch
):
    invoice, sender, treasury = await setup_chain(gateway, monkeypatch)
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(transfer(sender, "100", "resume-source"))
    target = str(Keypair.from_seed(bytes([10]) * 32).pubkey())
    signature = str(uuid4())
    adapter = AsyncMock()
    adapter.normalize_address = lambda x: x
    adapter.get_token_balance.return_value = Decimal("1000000")
    adapter.get_native_balance.return_value = Decimal("1000000")
    prepared = PreparedTransfer(
        "solana",
        signature,
        "signed-bytes",
        treasury,
        target,
        transfer(sender, "100", "mint").token_address,
        30000000,
        5000,
        {},
    )
    adapter.prepare_transfer.side_effect = [ChainRPCError("isolated unavailable"), prepared]
    adapter.broadcast_prepared.return_value = TransferReceipt("pending", signature)
    adapter.transfer_receipt.return_value = TransferReceipt("completed", signature, 5000)
    monkeypatch.setattr("src.services.onchain_gateway_service.adapter_for", lambda _: adapter)
    monkeypatch.setattr("src.workers.onchain_gateway.adapter_for", lambda _: adapter)
    async with gateway.factory() as session:
        result = await OnchainGatewayService(session).create_payout(
            gateway.merchant.id, "solana", "USDC", Decimal("30"), target, "crash-resume"
        )
        assert result["status"] == "preparing" and result["tx_id"] is None
    assert adapter.broadcast_prepared.call_count == 0
    from src.workers.onchain_gateway import reconcile_once

    await reconcile_once(gateway.factory)
    await reconcile_once(gateway.factory)
    async with gateway.factory() as session:
        from uuid import UUID

        assert (await session.get(OnchainOperation, UUID(result["id"]))).status == "completed"
    assert adapter.broadcast_prepared.call_count == 1


async def test_bad_rpc_retains_checkpoint_but_other_address_is_credited(gateway, monkeypatch):
    first, bad, _ = await setup_chain(gateway, monkeypatch)
    second, good, _ = await setup_chain(gateway, monkeypatch)
    async with gateway.factory() as session, session.begin():
        payments = (
            await session.scalars(
                select(PaymentSession).where(PaymentSession.invoice_id.in_([first.id, second.id]))
            )
        ).all()
        for payment in payments:
            session.add(
                OnchainScanCursor(
                    session_id=payment.id,
                    chain="solana",
                    last_block=19,
                    updated_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
    adapter = AsyncMock()
    adapter.get_latest_block.return_value = 20

    async def events(lower, upper, addresses):
        if addresses == [bad]:
            raise ChainRPCError("isolated RPC failure")
        return [transfer(good, "100", "valid-scan")]

    adapter.get_transfer_events.side_effect = events
    monkeypatch.setattr("src.workers.onchain_gateway.adapter_for", lambda _: adapter)
    from src.workers.onchain_gateway import scan_once

    await scan_once(gateway.factory)
    async with gateway.factory() as session:
        for payment in payments:
            cursor = await session.get(OnchainScanCursor, payment.id)
            assert cursor.last_block == (19 if payment.invoice_id == first.id else 20)
        assert (await session.get(type(second), second.id)).status == InvoiceStatus.CONFIRMED
        assert (await session.get(type(first), first.id)).status != InvoiceStatus.CONFIRMED


async def test_completed_sweep_is_final_even_when_gas_balance_is_empty(gateway, monkeypatch):
    invoice, sender, treasury = await setup_chain(gateway, monkeypatch)
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(transfer(sender, "100", "sweep-source"))
        payment = await session.scalar(
            select(PaymentSession).where(PaymentSession.invoice_id == invoice.id)
        )
        session.add(OnchainScanCursor(session_id=payment.id, chain="solana", last_block=20))
        job = await session.scalar(
            select(UnifiedSweepJob).where(UnifiedSweepJob.source_id == payment.id)
        )
        session.add(
            OnchainOperation(
                merchant_id=gateway.merchant.id,
                purpose="sweep",
                key=f"sweep:{job.id}",
                digest="a" * 64,
                chain="solana",
                sender=sender,
                destination=treasury,
                token=job.token_contract,
                amount_atomic=int(job.amount_raw),
                status="completed",
                tx_id=str(uuid4()),
                encrypted_payload="opaque",
                prepared_context={},
                fee_budget_atomic=5000,
            )
        )
    adapter = AsyncMock()
    adapter.normalize_address = lambda x: x
    monkeypatch.setattr("src.workers.onchain_gateway.adapter_for", lambda _: adapter)
    from src.workers.onchain_gateway import sweep_once

    await sweep_once(gateway.factory)
    async with gateway.factory() as session:
        assert (await session.get(UnifiedSweepJob, job.id)).state == SweepState.COMPLETED
    adapter.get_native_balance.assert_not_called()


async def test_lost_broadcast_replays_identical_signed_bytes_only(gateway, monkeypatch):
    invoice, sender, treasury = await setup_chain(gateway, monkeypatch)
    async with gateway.factory() as session, session.begin():
        await OnchainGatewayService(session).consume_event(
            transfer(sender, "100", "rebroadcast-source")
        )
    target = str(Keypair.from_seed(bytes([10]) * 32).pubkey())
    signature = str(uuid4())
    adapter = AsyncMock()
    adapter.normalize_address = lambda x: x
    adapter.get_token_balance.return_value = adapter.get_native_balance.return_value = Decimal(
        "1000000"
    )
    prepared = PreparedTransfer(
        "solana",
        signature,
        "same-signed-bytes",
        treasury,
        target,
        transfer(sender, "100", "mint").token_address,
        30000000,
        5000,
        {},
    )
    adapter.prepare_transfer.return_value = prepared
    adapter.broadcast_prepared.side_effect = [
        TransferReceipt("unknown", signature),
        TransferReceipt("pending", signature),
    ]
    adapter.transfer_receipt.return_value = TransferReceipt("unknown", signature)
    monkeypatch.setattr("src.services.onchain_gateway_service.adapter_for", lambda _: adapter)
    async with gateway.factory() as session:
        result = await OnchainGatewayService(session).create_payout(
            gateway.merchant.id, "solana", "USDC", Decimal("30"), target, "replay-signed"
        )
    from uuid import UUID

    async with gateway.factory() as session, session.begin():
        row = await session.get(OnchainOperation, UUID(result["id"]))
        row.last_broadcast_at = datetime.now(UTC) - timedelta(minutes=2)
    async with gateway.factory() as session:
        result = await OnchainGatewayService(session).reconcile(
            gateway.merchant.id, UUID(result["id"]), adapter
        )
        assert result["status"] == "pending"
    assert adapter.prepare_transfer.call_count == 1
    assert adapter.broadcast_prepared.call_count == 2
    assert (
        adapter.broadcast_prepared.call_args_list[0].args[0]
        == adapter.broadcast_prepared.call_args_list[1].args[0]
    )
    adapter.transfer_receipt.return_value = TransferReceipt("completed", signature, 5000)
    async with gateway.factory() as session:
        await OnchainGatewayService(session).reconcile(
            gateway.merchant.id, UUID(result["id"]), adapter
        )


async def test_gas_cannot_spend_another_merchants_native_entitlement(gateway, monkeypatch):
    from types import SimpleNamespace

    first, sender, _ = await setup_chain(gateway, monkeypatch, symbol="SOL", nominal="1")
    other = SimpleNamespace(factory=gateway.factory, merchant=gateway.merchants[1])
    second, other_sender, treasury = await setup_chain(
        other, monkeypatch, symbol="SOL", nominal="1"
    )
    async with gateway.factory() as session, session.begin():
        service = OnchainGatewayService(session)
        await service.consume_event(transfer(sender, "1", "gas-a", symbol="SOL"))
        await service.consume_event(transfer(other_sender, "1", "gas-b", symbol="SOL"))
        native = await service.asset("solana", "SOL")
        owed = sum(max(0, value) for value in (await service._entitlements(native.id)).values())
    total = Decimal(owed) / 10**9
    target = str(Keypair.from_seed(bytes([10]) * 32).pubkey())
    signature = str(uuid4())
    adapter = AsyncMock()
    adapter.normalize_address = lambda x: x
    adapter.get_native_balance.return_value = total
    adapter.prepare_transfer.return_value = PreparedTransfer(
        "solana", signature, "bytes", treasury, target, "", 10**9, 5000, {}
    )
    adapter.broadcast_prepared.return_value = TransferReceipt("pending", signature)
    adapter.transfer_receipt.return_value = TransferReceipt("completed", signature, 5000)
    monkeypatch.setattr("src.services.onchain_gateway_service.adapter_for", lambda _: adapter)
    async with gateway.factory() as session:
        quote = await OnchainGatewayService(session).balance(
            gateway.merchant.id, "solana", "SOL", adapter
        )
        assert Decimal(quote["available"]) == 0 and quote["reason"] == "operator_gas_not_ready"
        with pytest.raises(GatewayError):
            await OnchainGatewayService(session).create_payout(
                gateway.merchant.id, "solana", "SOL", Decimal("1"), target, "no-sponsored-gas"
            )
    adapter.prepare_transfer.assert_not_called()
    adapter.get_native_balance.return_value = total + Decimal("0.01")
    async with gateway.factory() as session:
        result = await OnchainGatewayService(session).create_payout(
            gateway.merchant.id, "solana", "SOL", Decimal("1"), target, "sponsored-gas"
        )
    from uuid import UUID

    async with gateway.factory() as session:
        await OnchainGatewayService(session).reconcile(
            gateway.merchant.id, UUID(result["id"]), adapter
        )
        row = await session.get(OnchainOperation, UUID(result["id"]))
        assert row.gas_transaction_id and int(row.native_cost_atomic) == 5000
    adapter.get_native_balance.return_value = (
        total + Decimal("0.01") - Decimal("1") - Decimal("0.000005")
    )
    async with gateway.factory() as session:
        quote = await OnchainGatewayService(session).balance(
            other.merchant.id, "solana", "SOL", adapter
        )
        assert Decimal(quote["available"]) == 1


async def test_feature_rollback_does_not_route_enrolled_jobs_to_legacy_workers(
    gateway, monkeypatch
):
    from fastapi import HTTPException

    from src.api.admin.router import guard_gateway_sweep_retry
    from src.blockchain.chains import get_chain_config
    from src.workers.evm_log_poller import get_active_deposit_addresses
    from src.workers.unified_sweeper import UnifiedBatchSweeper

    monkeypatch.setenv("ONCHAIN_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    records = []
    async with gateway.factory() as session:
        for enrolled in (False, True):
            invoice = await InvoiceService(session).create_invoice(
                gateway.merchant, Decimal(100), "USDT", ["bsc"], idempotency_key=str(uuid4())
            )
            address = "0x" + uuid4().hex + "12345678"
            deposit = DepositAddress(
                address=address,
                encrypted_privkey=b"inert",
                chain_group="evm",
                derivation_path=str(uuid4()),
                derivation_index=uuid4().int % 2**31,
            )
            session.add(deposit)
            await session.flush()
            payment = PaymentSession(
                invoice_id=invoice.id,
                chain="bsc",
                token="USDT",
                deposit_address_id=deposit.id,
                expires_at=invoice.expires_at,
            )
            session.add(payment)
            await session.flush()
            if enrolled:
                session.add(OnchainScanCursor(session_id=payment.id, chain="bsc", last_block=10))
            job = UnifiedSweepJob(
                source="invoice",
                source_id=payment.id,
                chain="bsc",
                token="USDT",
                token_contract=get_chain_config("bsc").tokens["USDT"].contract_address,
                from_address=address,
                to_address="0x" + "11" * 20,
                encrypted_private_key="inert",
                amount=Decimal(100),
                amount_raw=str(100 * 10**18),
                state=SweepState.PENDING_GAS,
            )
            session.add(job)
            await session.flush()
            records.append((address.lower(), job.id))
        await session.commit()
    async with gateway.factory() as session:
        active = await get_active_deposit_addresses(session, "bsc")
        assert records[0][0] in active and records[1][0] not in active
        pending = await UnifiedBatchSweeper()._get_pending_jobs_by_chain(session, 1000)
        selected = {job.id for jobs in pending.values() for job in jobs}
        assert records[0][1] in selected and records[1][1] not in selected
        assert not await guard_gateway_sweep_retry(
            session, await session.get(UnifiedSweepJob, records[0][1])
        )
        with pytest.raises(HTTPException) as rejected:
            await guard_gateway_sweep_retry(
                session, await session.get(UnifiedSweepJob, records[1][1])
            )
        assert rejected.value.status_code == 409


async def test_enrolment_detection_is_compatible_with_the_0011_schema():
    import os

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.db.session import has_onchain_scan_schema

    url = os.environ.get("PROVIDER_INGESTION_RUNTIME_DATABASE_URL")
    if not url:
        pytest.skip("Isolated legacy runtime database is not configured")
    engine = create_async_engine(url)
    assert engine.url.host in ("127.0.0.1", "localhost") and engine.url.database.startswith("test_")
    try:
        async with AsyncSession(engine) as session:
            assert not await has_onchain_scan_schema(session)
    finally:
        await engine.dispose()
