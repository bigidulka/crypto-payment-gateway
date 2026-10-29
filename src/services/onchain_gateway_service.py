"""Canonical chain receipts, tenant ledger balances and durable signed payouts."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.blockchain.chains import ChainType, get_chain_config, is_chain_supported
from src.blockchain.prepared import ChainEvidenceError, ChainRPCError, PreparedTransfer
from src.core.config import get_settings
from src.crypto.encryption import decrypt_secret, encrypt_secret
from src.db.models import (
    DepositAddress,
    DepositAddressLeaseStatus,
    Invoice,
    InvoiceEvent,
    InvoiceStatus,
    LedgerAccount,
    LedgerAsset,
    Merchant,
    OnchainOperation,
    OnchainReceipt,
    OnchainTx,
    OutboxStatus,
    OutboxWebhook,
    PaymentSession,
    PaymentSessionStatus,
    SweepSource,
    SweepState,
    TxStatus,
    UnifiedSweepJob,
    Webhook,
)
from src.db.models.ledger import LedgerDirection
from src.ledger.amounts import atomic_to_decimal, decimal_to_atomic
from src.services.gateway_provider_service import GatewayError
from src.services.ledger_posting_service import LedgerLine, LedgerPostingService

ACTIVE = ("preparing", "pending", "unknown", "reconciliation_required")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def adapter_for(chain):
    config = get_chain_config(chain)
    if config.chain_type == ChainType.SOLANA:
        from src.blockchain.solana_adapter import SolanaAdapter

        return SolanaAdapter(chain, config)
    if config.chain_type == ChainType.TON:
        from src.blockchain.ton_adapter import TonAdapter

        return TonAdapter(chain, config)
    from src.blockchain.evm_prepared import PreparedEvmAdapter

    return PreparedEvmAdapter(chain, config)


def projection(operation):
    return {
        "id": str(operation.id),
        "kind": operation.purpose,
        "chain": operation.chain,
        "asset_id": str(operation.asset_id) if operation.asset_id else None,
        "amount_atomic": str(operation.amount_atomic),
        "destination": operation.destination,
        "status": operation.status,
        "tx_id": operation.tx_id,
        "reason": operation.last_error,
        "wallet_released": operation.wallet_released,
        "native_cost_atomic": str(operation.native_cost_atomic)
        if operation.native_cost_atomic is not None
        else None,
        "fee_budget_atomic": str(operation.fee_budget_atomic)
        if operation.fee_budget_atomic is not None
        else None,
        "fee_paid_atomic": str(operation.fee_paid_atomic)
        if operation.fee_paid_atomic is not None
        else None,
    }


class OnchainGatewayService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    def guard(self, merchant_id, *, payout=False):
        if (
            not self.settings.onchain_gateway_enabled
            or not self.settings.ledger_rail_orchestration_enabled
            or str(merchant_id) not in self.settings.ledger_rail_orchestration_merchant_ids
        ):
            raise GatewayError("Gateway ledger is disabled for this merchant", status_code=503)
        if payout and not self.settings.onchain_payout_enabled:
            raise GatewayError("On-chain payout is disabled", status_code=503)

    async def _wallet_lock(self, chain, sender):
        lock = int.from_bytes(
            hashlib.sha256(f"{chain}:{sender}".encode()).digest()[:8], "big", signed=True
        )
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})

    async def accounts(self, merchant_id, *, gas=False):
        await self.session.scalar(
            select(Merchant.id).where(Merchant.id == merchant_id).with_for_update()
        )
        result = {}
        kinds = (
            ("operator_gas_expense", "operator_gas_cash")
            if gas
            else ("gateway_treasury_pending", "merchant_payable")
        )
        for kind in kinds:
            account = await self.session.scalar(
                select(LedgerAccount).where(
                    LedgerAccount.merchant_id == merchant_id, LedgerAccount.account_type == kind
                )
            )
            if account is None:
                account = LedgerAccount(
                    merchant_id=merchant_id,
                    account_type=kind,
                    custody_type="system"
                    if gas
                    else "gateway_managed"
                    if kind == "gateway_treasury_pending"
                    else "merchant_liability",
                )
                self.session.add(account)
                await self.session.flush()
            result[kind] = account
        return result

    async def asset(self, chain, symbol, *, allow_disabled=False):
        if not allow_disabled and not is_chain_supported(chain):
            raise GatewayError("Chain is disabled or unknown", status_code=422)
        config = get_chain_config(chain)
        token = config.get_asset_contract(symbol) if not config.is_native_asset(symbol) else ""
        if config.chain_type == ChainType.TON and token:
            from src.blockchain.ton_keys import normalize_ton_address

            token = normalize_ton_address(token)
        elif config.chain_type == ChainType.EVM:
            token = token.lower()
        values = dict(
            network_kind=config.chain_type.value,
            network_identifier=chain,
            canonical_identifier=token,
            is_native=not token,
            atomic_decimals=config.get_asset_decimals(symbol),
            symbol=symbol,
        )
        await self.session.execute(
            insert(LedgerAsset).values(id=uuid.uuid4(), **values).on_conflict_do_nothing()
        )
        asset = await self.session.scalar(
            select(LedgerAsset).where(
                LedgerAsset.network_kind == config.chain_type.value,
                LedgerAsset.network_identifier == chain,
                LedgerAsset.canonical_identifier == token,
                LedgerAsset.is_native == (not token),
            )
        )
        if asset.atomic_decimals != values["atomic_decimals"] or asset.symbol != symbol:
            raise GatewayError("Canonical chain asset conflicts", status_code=409)
        return asset

    async def _entitlements(self, asset_id):
        rows = await self.session.execute(
            text("""SELECT a.merchant_id,
            COALESCE(sum(CASE WHEN e.direction='credit' THEN e.amount_atomic
                ELSE -e.amount_atomic END),0)
            FROM ledger_accounts a JOIN ledger_entries e ON e.ledger_account_id=a.id
            JOIN ledger_transactions t ON t.id=e.ledger_transaction_id
            WHERE a.account_type='merchant_payable'
                AND e.ledger_asset_id=:asset AND t.status='posted'
            GROUP BY a.merchant_id"""),
            {"asset": asset_id},
        )
        return {row[0]: int(row[1]) for row in rows}

    async def balance(self, merchant_id, chain, symbol, adapter=None):
        self.guard(merchant_id)
        asset = await self.asset(chain, symbol)
        await self.accounts(merchant_id)
        entitlements = await self._entitlements(asset.id)
        own = max(0, entitlements.get(merchant_id, 0))
        reserved = int(
            await self.session.scalar(
                select(func.coalesce(func.sum(OnchainOperation.amount_atomic), 0)).where(
                    OnchainOperation.merchant_id == merchant_id,
                    OnchainOperation.asset_id == asset.id,
                    OnchainOperation.purpose == "payout",
                    OnchainOperation.status.in_(ACTIVE),
                )
            )
            or 0
        )
        others = sum(max(0, v) for m, v in entitlements.items() if m != merchant_id)
        address = self.settings.get_treasury_address(chain)
        available, wallet, gas_liquid, reason = None, None, None, "treasury_unconfigured"
        owned_adapter = adapter is None
        adapter = adapter or adapter_for(chain)
        try:
            if address:
                address = adapter.normalize_address(address)
                value = (
                    await adapter.get_token_balance(address, asset.canonical_identifier)
                    if asset.canonical_identifier
                    else await adapter.get_native_balance(address)
                )
                wallet = decimal_to_atomic(value, asset.atomic_decimals)
                available = max(0, min(own - reserved, wallet - others - reserved))
                reason = None
                config = get_chain_config(chain)
                native_asset = (
                    asset if asset.is_native else await self.asset(chain, config.native_symbol)
                )
                native_owed = sum(
                    max(0, value) for value in (await self._entitlements(native_asset.id)).values()
                )
                native_wallet = (
                    wallet
                    if asset.is_native
                    else decimal_to_atomic(
                        await adapter.get_native_balance(address), config.native_decimals
                    )
                )
                gas_liquid = max(0, native_wallet - native_owed)
                if gas_liquid == 0:
                    available, reason = 0, "operator_gas_not_ready"
        except (ChainRPCError, ChainEvidenceError, ValueError):
            reason = "authoritative_balance_unavailable"
        finally:
            if owned_adapter:
                await adapter.close()
        return {
            "chain": chain,
            "asset": symbol,
            "asset_id": str(asset.id),
            "decimals": asset.atomic_decimals,
            "ledger_atomic": str(own),
            "reserved_atomic": str(reserved),
            "wallet_atomic": str(wallet) if wallet is not None else None,
            "available": str(atomic_to_decimal(available, asset.atomic_decimals))
            if available is not None
            else None,
            "reason": reason,
            "operator_gas_liquidity_atomic": str(gas_liquid) if gas_liquid is not None else None,
        }

    async def create_payout(self, merchant_id, chain, symbol, amount, destination, key):
        if not key or len(key) > 128:
            raise GatewayError("Persistent idempotency key is required", status_code=422)
        # Read an owned durable intent even after its rail or feature was paused.
        existing = await self.session.scalar(
            select(OnchainOperation).where(
                OnchainOperation.merchant_id == merchant_id, OnchainOperation.key == key
            )
        )
        if existing:
            asset = (
                await self.session.get(LedgerAsset, existing.asset_id)
                if existing.asset_id
                else None
            )
            if (
                existing.purpose != "payout"
                or existing.chain != chain
                or not asset
                or asset.symbol != symbol
            ):
                raise GatewayError("Payout key was used with a different request", status_code=409)
            adapter = adapter_for(existing.chain)
            try:
                raw = decimal_to_atomic(amount, asset.atomic_decimals)
                destination = adapter.normalize_address(destination)
                if existing.digest != digest(
                    [chain, symbol, str(raw), existing.sender, destination]
                ):
                    raise GatewayError(
                        "Payout key was used with a different request", status_code=409
                    )
                return projection(existing)
            finally:
                await adapter.close()
        self.guard(merchant_id, payout=True)
        if not is_chain_supported(chain):
            raise GatewayError("Chain is disabled or unknown", status_code=422)
        config = get_chain_config(chain)
        adapter = adapter_for(chain)
        try:
            sender = adapter.normalize_address(self.settings.get_treasury_address(chain))
            destination = adapter.normalize_address(destination)
            raw = decimal_to_atomic(amount, config.get_asset_decimals(symbol))
            request_digest = digest([chain, symbol, str(raw), sender, destination])
            async with self.session.begin_nested():
                await self._wallet_lock(chain, sender)
                await self.accounts(merchant_id)
                existing = await self.session.scalar(
                    select(OnchainOperation).where(
                        OnchainOperation.merchant_id == merchant_id, OnchainOperation.key == key
                    )
                )
                if existing:
                    if existing.digest != request_digest:
                        raise GatewayError(
                            "Payout key was used with a different request", status_code=409
                        )
                    return projection(existing)
                pending = await self.session.scalar(
                    select(OnchainOperation.id).where(
                        OnchainOperation.chain == chain,
                        OnchainOperation.sender == sender,
                        OnchainOperation.status.in_(ACTIVE),
                        OnchainOperation.wallet_released.is_(False),
                    )
                )
                if pending:
                    raise GatewayError("Treasury has an unresolved transaction", status_code=409)
                quote = await self.balance(merchant_id, chain, symbol, adapter)
                if quote["available"] is None:
                    raise GatewayError("Authoritative liquidity is unavailable", status_code=503)
                if amount <= 0 or amount > Decimal(quote["available"]):
                    raise GatewayError("Insufficient verified available balance", status_code=422)
                asset = await self.asset(chain, symbol)
                operation = OnchainOperation(
                    merchant_id=merchant_id,
                    purpose="payout",
                    key=key,
                    digest=request_digest,
                    chain=chain,
                    sender=sender,
                    destination=destination,
                    token=asset.canonical_identifier,
                    asset_id=asset.id,
                    amount_atomic=raw,
                    status="preparing",
                )
                self.session.add(operation)
                await self.session.flush()
            await self.session.commit()  # reservation is durable BEFORE signing or broadcast
            private = getattr(
                self.settings,
                f"{chain}_treasury_private_key",
                self.settings.evm_treasury_private_key,
            ).get_secret_value()
            await self.submit(operation.id, private, adapter)
            return projection(operation)
        finally:
            await adapter.close()

    async def submit(self, operation_id, private, adapter):
        operation = await self.session.scalar(
            select(OnchainOperation).where(OnchainOperation.id == operation_id).with_for_update()
        )
        if operation is None:
            raise GatewayError("Operation not found", status_code=404)
        self.guard(operation.merchant_id, payout=operation.purpose == "payout")
        if operation.status != "preparing" or operation.tx_id:
            return operation
        operation.updated_at = datetime.now(UTC)
        await self._wallet_lock(operation.chain, operation.sender)
        others = await self.session.scalar(
            select(OnchainOperation.id).where(
                OnchainOperation.chain == operation.chain,
                OnchainOperation.sender == operation.sender,
                OnchainOperation.id != operation.id,
                OnchainOperation.status.in_(ACTIVE),
                OnchainOperation.wallet_released.is_(False),
            )
        )
        if others:
            await self.session.commit()
            return operation
        try:
            config = get_chain_config(operation.chain)
            symbol = (
                config.native_symbol
                if not operation.token
                else next(
                    t.symbol
                    for t in config.tokens.values()
                    if adapter.normalize_address(t.contract_address)
                    == adapter.normalize_address(operation.token)
                )
            )
            decimals = config.get_asset_decimals(symbol)
            if not private:
                raise ChainEvidenceError("Signing key is not configured")
            prepared = await adapter.prepare_transfer(
                private,
                operation.destination,
                atomic_to_decimal(int(operation.amount_atomic), decimals),
                operation.token or None,
                reference=str(operation.id),
            )
            if prepared.sender != operation.sender or prepared.amount_raw != int(
                operation.amount_atomic
            ):
                raise ChainEvidenceError("Signing key or amount binding differs")
            if operation.purpose == "payout":
                native_asset = await self.asset(operation.chain, config.native_symbol)
                liabilities = sum(
                    max(0, value) for value in (await self._entitlements(native_asset.id)).values()
                )
                wallet = decimal_to_atomic(
                    await adapter.get_native_balance(operation.sender), config.native_decimals
                )
                if wallet - liabilities < prepared.fee_budget_raw:
                    raise ChainEvidenceError("Separate operator gas liquidity is insufficient")
        except ChainRPCError:
            operation.last_error = "preparation_rpc_unavailable"
            await self.session.commit()
            return operation
        except (ChainEvidenceError, ValueError, KeyError, StopIteration):
            operation.status, operation.last_error = "failed", "preparation_rejected"
            await self.session.commit()
            return operation
        operation.tx_id = prepared.tx_id
        operation.encrypted_payload = encrypt_secret(
            prepared.payload, self.settings.encryption_key.get_secret_value()
        ).hex()
        operation.prepared_context = prepared.context
        operation.fee_budget_atomic = prepared.fee_budget_raw
        operation.status = "unknown"
        operation.last_broadcast_at = datetime.now(UTC)
        await self.session.commit()  # signed identity and reserve survive a lost response/crash
        result = await adapter.broadcast_prepared(prepared)
        operation.status = result.status
        operation.last_error = result.reason
        await self.session.commit()
        return operation

    def prepared(self, operation):
        return PreparedTransfer(
            operation.chain,
            operation.tx_id,
            decrypt_secret(
                bytes.fromhex(operation.encrypted_payload),
                self.settings.encryption_key.get_secret_value(),
            ),
            operation.sender,
            operation.destination,
            operation.token,
            int(operation.amount_atomic),
            int(operation.fee_budget_atomic),
            operation.prepared_context,
        )

    async def reconcile(self, merchant_id, operation_id, adapter=None):
        operation = await self.session.scalar(
            select(OnchainOperation)
            .where(OnchainOperation.id == operation_id, OnchainOperation.merchant_id == merchant_id)
            .with_for_update()
        )
        if not operation:
            raise GatewayError("Operation not found", status_code=404)
        if operation.status not in ACTIVE or not operation.tx_id:
            return projection(operation)
        owned = adapter is None
        adapter = adapter or adapter_for(operation.chain)
        try:
            return await self._reconcile_prepared(operation, adapter)
        finally:
            if owned:
                await adapter.close()

    async def _reconcile_prepared(self, operation, adapter):
        operation.updated_at = datetime.now(UTC)
        try:
            prepared = self.prepared(operation)
            result = await adapter.transfer_receipt(prepared)
            if (
                result.status == "unknown"
                and self.settings.onchain_gateway_enabled
                and self.settings.ledger_rail_orchestration_enabled
                and str(operation.merchant_id)
                in self.settings.ledger_rail_orchestration_merchant_ids
                and (operation.purpose != "payout" or self.settings.onchain_payout_enabled)
                and not operation.wallet_released
                and (
                    operation.last_broadcast_at is None
                    or datetime.now(UTC) - operation.last_broadcast_at
                    >= timedelta(seconds=self.settings.onchain_rebroadcast_interval_seconds)
                )
            ):
                # Replay the same signature/nonce/body, never prepare replacement bytes.
                operation.last_broadcast_at = datetime.now(UTC)
                await self.session.commit()
                result = await adapter.broadcast_prepared(prepared)
        except ChainRPCError:
            await self.session.commit()
            return projection(operation)
        except (ChainEvidenceError, ValueError, KeyError):
            operation.status, operation.last_error = (
                "reconciliation_required",
                "receipt_binding_unavailable",
            )
            await self.session.commit()
            return projection(operation)
        if result.tx_id != operation.tx_id:
            operation.status, operation.last_error = (
                "reconciliation_required",
                "receipt_identity_differs",
            )
            await self.session.commit()
            return projection(operation)
        cost = result.native_cost_raw if result.native_cost_raw is not None else result.fee_raw
        if cost is not None and (cost < 0 or cost > int(operation.fee_budget_atomic)):
            operation.status, operation.last_error = (
                "reconciliation_required",
                "native_cost_exceeds_budget",
            )
            await self.session.commit()
            return projection(operation)
        if result.status == "completed" and operation.purpose == "payout":
            accounts = await self.accounts(operation.merchant_id)
            posting = await LedgerPostingService(self.session).post_in_transaction(
                merchant_id=operation.merchant_id,
                source_namespace="onchain_payout",
                source_external_id=f"{operation.chain}:{operation.tx_id}",
                source_digest=operation.digest,
                idempotency_key=f"onchain-payout:{operation.id}",
                lines=[
                    LedgerLine(
                        accounts["merchant_payable"].id,
                        operation.asset_id,
                        LedgerDirection.DEBIT,
                        int(operation.amount_atomic),
                    ),
                    LedgerLine(
                        accounts["gateway_treasury_pending"].id,
                        operation.asset_id,
                        LedgerDirection.CREDIT,
                        int(operation.amount_atomic),
                    ),
                ],
            )
            operation.transaction_id = posting.id
        if result.status in ("completed", "failed") and cost:
            config = get_chain_config(operation.chain)
            native = await self.asset(operation.chain, config.native_symbol, allow_disabled=True)
            gas_accounts = await self.accounts(operation.merchant_id, gas=True)
            posting = await LedgerPostingService(self.session).post_in_transaction(
                merchant_id=operation.merchant_id,
                source_namespace="onchain_gas",
                source_external_id=f"{operation.chain}:{operation.tx_id}",
                source_digest=operation.digest,
                idempotency_key=f"onchain-gas:{operation.id}",
                lines=[
                    LedgerLine(
                        gas_accounts["operator_gas_expense"].id,
                        native.id,
                        LedgerDirection.DEBIT,
                        cost,
                    ),
                    LedgerLine(
                        gas_accounts["operator_gas_cash"].id,
                        native.id,
                        LedgerDirection.CREDIT,
                        cost,
                    ),
                ],
            )
            operation.gas_transaction_id = posting.id
        operation.status, operation.last_error = result.status, result.reason
        operation.wallet_released = operation.wallet_released or result.wallet_released
        operation.fee_paid_atomic, operation.native_cost_atomic = result.fee_raw, cost
        await self.session.commit()
        return projection(operation)

    async def consume_event(self, event, *, session_id=None):
        """Caller owns the SQL transaction; checkpoint and all projections commit together."""
        config = get_chain_config(event.chain)
        address = event.to_address
        statement = (
            select(PaymentSession)
            .join(DepositAddress)
            .where(PaymentSession.chain == event.chain, DepositAddress.address == address)
            .options(selectinload(PaymentSession.deposit_address))
            .with_for_update(of=PaymentSession)
        )
        if session_id is not None:
            statement = statement.where(PaymentSession.id == session_id)
        candidates = (await self.session.scalars(statement)).all()
        if len(candidates) > 1:
            raise ChainEvidenceError("Deposit address has ambiguous invoice history")
        payment = candidates[0] if candidates else None
        if payment is None:
            return
        invoice = await self.session.scalar(
            select(Invoice).where(Invoice.id == payment.invoice_id).with_for_update()
        )
        self.guard(invoice.merchant_id)
        existing = await self.session.scalar(
            select(OnchainTx).where(
                OnchainTx.chain == event.chain,
                OnchainTx.tx_hash == event.tx_hash,
                OnchainTx.log_index == event.log_index,
            )
        )
        if existing:
            if existing.amount != event.amount or existing.to_address != event.to_address:
                raise ChainEvidenceError("Previously recorded chain observation changed")
            return
        expected_contract = (
            config.get_asset_contract(payment.token)
            if not config.is_native_asset(payment.token)
            else ""
        )
        if config.chain_type == ChainType.TON and expected_contract:
            from src.blockchain.ton_keys import normalize_ton_address

            expected_contract = normalize_ton_address(expected_contract)
        if event.token_address != expected_contract:
            payment.mismatch_reason, payment.mismatch_token = "wrong_token", event.token_symbol
            return
        if event.reference and event.reference != invoice.public_id:
            payment.mismatch_reason = "wrong_reference"
            return
        fact = OnchainTx(
            chain=event.chain,
            tx_hash=event.tx_hash,
            block_number=event.block_number,
            log_index=event.log_index,
            from_address=event.from_address,
            to_address=event.to_address,
            token_contract=event.token_address,
            amount=event.amount,
            payment_session_id=payment.id,
            status=TxStatus.CONFIRMED,
            confirmations=config.confirmations,
            confirmed_at=datetime.now(UTC),
        )
        self.session.add(fact)
        await self.session.flush()
        facts = (
            await self.session.scalars(
                select(OnchainTx).where(
                    OnchainTx.payment_session_id == payment.id,
                    OnchainTx.status == TxStatus.CONFIRMED,
                )
            )
        ).all()
        decimals = config.get_asset_decimals(payment.token)
        total = sum(decimal_to_atomic(f.amount, decimals) for f in facts)
        payment.received_amount = atomic_to_decimal(total, decimals)
        receipt = await self.session.scalar(
            select(OnchainReceipt).where(OnchainReceipt.invoice_id == invoice.id)
        )
        if receipt:
            # Additional deposits remain visible, but cannot silently rewrite a completed receipt.
            payment.mismatch_reason = "additional_payment_requires_reconciliation"
            return
        late = event.timestamp is None or event.timestamp > int(invoice.expires_at.timestamp())
        if late or invoice.status == InvoiceStatus.EXPIRED:
            payment.status, payment.mismatch_reason = PaymentSessionStatus.LATE, "late_payment"
            return
        if total < decimal_to_atomic(invoice.amount, decimals):
            payment.mismatch_reason = "underpaid"
            return
        asset = await self.asset(event.chain, payment.token)
        accounts = await self.accounts(invoice.merchant_id)
        evidence = sorted(
            [
                {
                    "tx_id": f.tx_hash,
                    "index": f.log_index,
                    "amount_atomic": str(decimal_to_atomic(f.amount, decimals)),
                }
                for f in facts
            ],
            key=lambda f: (f["tx_id"], f["index"]),
        )
        economic = digest([str(payment.id), str(asset.id), evidence])
        posting = await LedgerPostingService(self.session).post_in_transaction(
            merchant_id=invoice.merchant_id,
            source_namespace="onchain_payment",
            source_external_id=f"{event.chain}:{payment.id}",
            source_digest=economic,
            idempotency_key=f"onchain-payment:{payment.id}",
            lines=[
                LedgerLine(
                    accounts["gateway_treasury_pending"].id, asset.id, LedgerDirection.DEBIT, total
                ),
                LedgerLine(
                    accounts["merchant_payable"].id, asset.id, LedgerDirection.CREDIT, total
                ),
            ],
        )
        event_id = "onchain_" + economic[:56]
        invoice.status, payment.status = InvoiceStatus.CONFIRMED, PaymentSessionStatus.PAID
        payment.mismatch_reason = None
        payment.paid_at = datetime.now(UTC)
        payment.deposit_address.lease_status = DepositAddressLeaseStatus.RETIRED
        payment.deposit_address.retired_at = datetime.now(UTC)
        payload = {
            "event_id": event_id,
            "event": "invoice.confirmed",
            "invoice_id": str(invoice.id),
            "public_id": invoice.public_id,
            "amount": str(atomic_to_decimal(total, decimals)),
            "asset": payment.token,
            "metadata": invoice.extra_data or {},
            "observed": {
                "network_kind": asset.network_kind,
                "network_identifier": asset.network_identifier,
                "asset_id": str(asset.id),
                "amount_atomic": str(total),
                "atomic_decimals": decimals,
            },
        }
        self.session.add(
            InvoiceEvent(
                invoice_id=invoice.id,
                event_id=event_id,
                event_type="invoice.confirmed",
                payload=payload,
            )
        )
        webhooks = (
            await self.session.scalars(
                select(Webhook).where(
                    Webhook.merchant_id == invoice.merchant_id, Webhook.is_active.is_(True)
                )
            )
        ).all()
        for hook in webhooks:
            if "invoice.confirmed" in hook.events:
                self.session.add(
                    OutboxWebhook(
                        webhook_id=hook.id,
                        invoice_id=invoice.id,
                        event_type="invoice.confirmed",
                        payload=payload,
                        status=OutboxStatus.PENDING,
                    )
                )
        self.session.add(
            UnifiedSweepJob(
                source=SweepSource.INVOICE,
                source_id=payment.id,
                chain=event.chain,
                token=payment.token,
                token_contract=expected_contract,
                from_address=event.to_address,
                to_address=self.settings.get_treasury_address(event.chain),
                encrypted_private_key=payment.deposit_address.encrypted_privkey.hex(),
                amount=atomic_to_decimal(total, decimals),
                amount_raw=str(total),
                state=SweepState.PENDING_GAS,
            )
        )
        await self.session.flush()
        self.session.add(
            OnchainReceipt(
                merchant_id=invoice.merchant_id,
                invoice_id=invoice.id,
                session_id=payment.id,
                asset_id=asset.id,
                transaction_id=posting.id,
                event_id=event_id,
                amount_atomic=total,
                evidence={"digest": economic, "transfers": evidence},
            )
        )
        await self.session.flush()
