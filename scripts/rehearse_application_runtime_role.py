"""Exercise legacy invoice/outbox and ledger post on one non-owner DB connection.

LOCAL/DISPOSABLE ONLY. Requires DATABASE_RUNTIME_ROLE_ENABLED=true and
DATABASE_URL pointing at a test_* loopback non-owner database. A distinct
MIGRATION_DATABASE_URL is used only for the prior migration.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select

from src.db.models import LedgerAccount, LedgerAsset, Merchant, OutboxWebhook, Webhook
from src.db.models.ledger import LedgerDirection
from src.db.session import close_db, get_session_factory
from src.services.invoice_service import InvoiceService
from src.services.ledger_posting_service import LedgerLine, LedgerPostingService


async def main() -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        suffix = str(uuid.uuid4())
        merchant = Merchant(name="app role", email=f"app-role-{suffix}@test.invalid")
        session.add(merchant)
        await session.flush()
        session.add(
            Webhook(
                merchant_id=merchant.id,
                url="https://example.test/queue",
                secret="x" * 32,
                events=["invoice.created"],
            )
        )
        asset = LedgerAsset(
            network_kind="evm",
            network_identifier="8453",
            canonical_identifier="0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8],
            is_native=False,
            atomic_decimals=6,
            symbol="USDC",
        )
        session.add(asset)
        await session.flush()
        debit = LedgerAccount(
            merchant_id=merchant.id,
            account_type="gateway_treasury_pending",
            custody_type="gateway_managed",
        )
        credit = LedgerAccount(
            merchant_id=merchant.id,
            account_type="merchant_payable",
            custody_type="merchant_liability",
        )
        session.add_all([debit, credit])
        await session.commit()
        merchant_id, asset_id, debit_id, credit_id = (
            merchant.id,
            asset.id,
            debit.id,
            credit.id,
        )

        invoices = InvoiceService(session)
        invoice = await invoices.create_invoice(
            merchant,
            Decimal("12.34"),
            "USDC",
            ["base"],
            idempotency_key=f"app-role-invoice-{suffix}",
        )
        invoice_id = invoice.id
        fetched = await invoices.get_invoice(invoice_id, merchant_id)
        listed, total = await invoices.list_invoices(merchant_id)
        invoice_read_ok = fetched.id == invoice_id and total == 1 and len(listed) == 1
        await session.rollback()

        async with session.begin():
            transaction = await LedgerPostingService(session).post_in_transaction(
                merchant_id=merchant_id,
                source_namespace="app-role",
                source_external_id=f"same-connection-{suffix}",
                source_digest="a" * 64,
                idempotency_key=f"app-role-ledger-{suffix}",
                lines=(
                    LedgerLine(debit_id, asset_id, LedgerDirection.DEBIT, 42),
                    LedgerLine(credit_id, asset_id, LedgerDirection.CREDIT, 42),
                ),
            )
        outbox = await session.scalar(
            select(OutboxWebhook).where(OutboxWebhook.invoice_id == invoice_id)
        )
        assert invoice_read_ok
        assert outbox is not None and transaction.id
        print("APP_ROLE_LEGACY_LEDGER_OUTBOX_OK")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
