"""Create a known balanced posted ledger journal in a disposable database."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import asyncpg
from sqlalchemy.engine import make_url


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _require_disposable_database_url(database_url: str) -> str:
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host not in {"127.0.0.1", "::1"}:
        raise RuntimeError("DATABASE_URL must use a loopback postgresql+asyncpg URL")
    if not parsed.database or not parsed.database.startswith("test_"):
        raise RuntimeError("DATABASE_URL database must start with test_")
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> None:
    dsn = _require_disposable_database_url(os.environ["DATABASE_URL"])
    connection = await asyncpg.connect(dsn)
    try:
        merchant, asset, debit, credit, transaction = (uuid.uuid4() for _ in range(5))
        await connection.execute(
            "INSERT INTO merchants (id,name,email,is_active) VALUES ($1,'backup fixture','backup-fixture@test.invalid',true)",
            merchant,
        )
        await connection.execute(
            "INSERT INTO ledger_assets (id,network_kind,network_identifier,canonical_identifier,is_native,atomic_decimals,symbol) VALUES ($1,'evm','8453',$2,false,6,'USDC')",
            asset,
            "0x" + "b" * 40,
        )
        await connection.execute(
            "INSERT INTO ledger_accounts (id,merchant_id,account_type,custody_type) VALUES ($1,$2,'gateway_treasury_pending','gateway_managed'),($3,$2,'merchant_payable','merchant_liability')",
            debit,
            merchant,
            credit,
        )
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO ledger_transactions (id,merchant_id,status,source_namespace,source_external_id,source_digest,idempotency_key,posting_digest) VALUES ($1,$2,'open','backup-fixture',$3,$4,$5,$6)",
                transaction,
                merchant,
                str(transaction),
                digest("backup-source"),
                "backup-fixture-key",
                digest("backup-posting"),
            )
            await connection.execute(
                "INSERT INTO ledger_entries (id,merchant_id,ledger_transaction_id,ledger_account_id,ledger_asset_id,direction,amount_atomic) VALUES ($1,$2,$3,$4,$5,'debit',42),($6,$2,$3,$7,$5,'credit',42)",
                uuid.uuid4(),
                merchant,
                transaction,
                debit,
                asset,
                uuid.uuid4(),
                credit,
            )
            await connection.execute(
                "UPDATE ledger_transactions SET status='posted', posted_at=now() WHERE id=$1",
                transaction,
            )
            await connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        print(f"fixture_transaction_id={transaction}")
        print("fixture_balance_atomic=42")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
