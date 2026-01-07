#!/usr/bin/env python3
"""
Создание таблиц для User Wallets (Persistent Deposits).
Запускать внутри Docker контейнера или с правильным DATABASE_URL.
"""

import asyncio
import sys

sys.path.insert(0, "/app")

from sqlalchemy import text


async def create_tables():
    """Создать таблицы для user wallets."""
    from src.db.session import get_session_factory

    session_factory = get_session_factory()

    async with session_factory() as session:
        # Создаём таблицы
        await session.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS user_wallets (
                id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
                external_user_id TEXT NOT NULL,
                user_metadata TEXT,
                is_active INTEGER DEFAULT 1 NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                UNIQUE(merchant_id, external_user_id)
            )
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS ix_user_wallets_merchant_id 
            ON user_wallets(merchant_id)
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS ix_user_wallets_external_user_id 
            ON user_wallets(external_user_id)
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS wallet_addresses (
                id TEXT PRIMARY KEY,
                user_wallet_id TEXT NOT NULL REFERENCES user_wallets(id) ON DELETE CASCADE,
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                derivation_index INTEGER NOT NULL,
                encrypted_private_key TEXT NOT NULL,
                is_active INTEGER DEFAULT 1 NOT NULL,
                last_scanned_block INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                UNIQUE(user_wallet_id, chain),
                UNIQUE(chain, address)
            )
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS ix_wallet_addresses_chain 
            ON wallet_addresses(chain)
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS ix_wallet_addresses_address 
            ON wallet_addresses(address)
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS deposits (
                id TEXT PRIMARY KEY,
                user_wallet_id TEXT NOT NULL REFERENCES user_wallets(id) ON DELETE CASCADE,
                wallet_address_id TEXT NOT NULL REFERENCES wallet_addresses(id) ON DELETE CASCADE,
                chain TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                block_number INTEGER NOT NULL,
                log_index INTEGER NOT NULL,
                amount TEXT NOT NULL,
                asset TEXT NOT NULL,
                token_contract TEXT NOT NULL,
                from_address TEXT NOT NULL,
                status TEXT DEFAULT 'pending' NOT NULL,
                confirmations INTEGER DEFAULT 0 NOT NULL,
                required_confirmations INTEGER NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                confirmed_at TIMESTAMP,
                credited_at TIMESTAMP,
                sweep_tx_hash TEXT,
                swept_at TIMESTAMP,
                UNIQUE(chain, tx_hash, log_index)
            )
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS ix_deposits_status 
            ON deposits(status)
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE INDEX IF NOT EXISTS ix_deposits_chain 
            ON deposits(chain)
        """
            )
        )

        await session.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS user_balances (
                id TEXT PRIMARY KEY,
                user_wallet_id TEXT NOT NULL REFERENCES user_wallets(id) ON DELETE CASCADE,
                asset TEXT NOT NULL,
                balance TEXT DEFAULT '0' NOT NULL,
                total_deposited TEXT DEFAULT '0' NOT NULL,
                total_withdrawn TEXT DEFAULT '0' NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                UNIQUE(user_wallet_id, asset)
            )
        """
            )
        )

        await session.commit()
        print("✅ User wallet tables created successfully!")


if __name__ == "__main__":
    asyncio.run(create_tables())
