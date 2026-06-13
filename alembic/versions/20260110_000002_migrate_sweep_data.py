"""Migrate legacy sweep tables to unified_sweep_jobs.

Revision ID: 0006_migrate_sweep_data
Revises: 0005_unified_sweep
Create Date: 2026-01-10

Миграция данных из sweep_jobs и deposit_sweep_jobs в unified_sweep_jobs,
затем удаление старых таблиц.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0006_migrate_sweep_data'
down_revision = '0005_unified_sweep'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    
    # === 1. Мигрируем sweep_jobs (invoice payments) ===
    # Получаем данные из sweep_jobs с join на payment_sessions и deposit_addresses
    conn.execute(sa.text("""
        INSERT INTO unified_sweep_jobs (
            id,
            source,
            source_id,
            chain,
            token,
            token_contract,
            from_address,
            to_address,
            encrypted_private_key,
            amount,
            amount_raw,
            state,
            gas_tx_hash,
            sweep_tx_hash,
            attempts,
            max_attempts,
            last_error,
            next_retry_at,
            priority,
            created_at,
            updated_at,
            completed_at
        )
        SELECT 
            sj.id,
            'invoice'::sweep_source,
            sj.payment_session_id,
            ps.chain,
            ps.token,
            COALESCE(
                CASE 
                    WHEN ps.chain = 'base' AND ps.token = 'USDC' THEN '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
                    WHEN ps.chain = 'base' AND ps.token = 'USDT' THEN '0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2'
                    WHEN ps.chain = 'arbitrum' AND ps.token = 'USDC' THEN '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'
                    WHEN ps.chain = 'arbitrum' AND ps.token = 'USDT' THEN '0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9'
                    WHEN ps.chain = 'bsc' AND ps.token = 'USDC' THEN '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d'
                    WHEN ps.chain = 'bsc' AND ps.token = 'USDT' THEN '0x55d398326f99059fF775485246999027B3197955'
                    WHEN ps.chain = 'polygon' AND ps.token = 'USDC' THEN '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359'
                    WHEN ps.chain = 'polygon' AND ps.token = 'USDT' THEN '0xc2132D05D31c914a87C6611C10748AEb04B58e8F'
                    WHEN ps.chain = 'avax' AND ps.token = 'USDC' THEN '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E'
                    WHEN ps.chain = 'avax' AND ps.token = 'USDT' THEN '0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7'
                    WHEN ps.chain = 'optimism' AND ps.token = 'USDC' THEN '0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85'
                    WHEN ps.chain = 'optimism' AND ps.token = 'USDT' THEN '0x94b008aA00579c1307B0EF2c499aD98a8ce58e58'
                    ELSE '0x0000000000000000000000000000000000000000'
                END,
                '0x0000000000000000000000000000000000000000'
            ),
            da.address,
            -- Treasury address (из .env, но используем placeholder)
            '0x3ec68709334f64ee4927891627f0b395c6ff6754',
            encode(da.encrypted_privkey, 'hex'),
            COALESCE(i.amount, 0),
            COALESCE((i.amount * 1000000)::text, '0'),
            sj.state,
            sj.gas_tx_hash,
            sj.sweep_tx_hash,
            sj.attempts,
            sj.max_attempts,
            sj.last_error,
            sj.next_retry_at,
            CASE 
                WHEN i.amount >= 1000 THEN 100
                WHEN i.amount >= 100 THEN 50
                ELSE 10
            END,
            sj.created_at,
            sj.updated_at,
            CASE WHEN sj.state = 'completed' THEN sj.updated_at ELSE NULL END
        FROM sweep_jobs sj
        JOIN payment_sessions ps ON ps.id = sj.payment_session_id
        JOIN deposit_addresses da ON da.id = ps.deposit_address_id
        JOIN invoices i ON i.id = ps.invoice_id
        WHERE NOT EXISTS (
            SELECT 1 FROM unified_sweep_jobs usj 
            WHERE usj.source = 'invoice' AND usj.source_id = sj.payment_session_id
        )
    """))
    
    # === 2. Мигрируем deposit_sweep_jobs (persistent deposits) ===
    conn.execute(sa.text("""
        INSERT INTO unified_sweep_jobs (
            id,
            source,
            source_id,
            chain,
            token,
            token_contract,
            from_address,
            to_address,
            encrypted_private_key,
            amount,
            amount_raw,
            state,
            gas_tx_hash,
            sweep_tx_hash,
            attempts,
            max_attempts,
            last_error,
            next_retry_at,
            priority,
            created_at,
            updated_at,
            completed_at
        )
        SELECT 
            dsj.id,
            'persistent'::sweep_source,
            dsj.deposit_id,
            d.chain,
            d.asset,
            d.token_contract,
            wa.address,
            -- Treasury address
            '0x3ec68709334f64ee4927891627f0b395c6ff6754',
            wa.encrypted_private_key,
            d.amount,
            (d.amount * POWER(10, 6))::bigint::text,
            dsj.state,
            dsj.gas_tx_hash,
            dsj.sweep_tx_hash,
            dsj.attempts,
            dsj.max_attempts,
            dsj.last_error,
            dsj.next_retry_at,
            CASE 
                WHEN d.amount >= 1000 THEN 100
                WHEN d.amount >= 100 THEN 50
                ELSE 10
            END,
            dsj.created_at,
            dsj.updated_at,
            CASE WHEN dsj.state = 'completed' THEN dsj.updated_at ELSE NULL END
        FROM deposit_sweep_jobs dsj
        JOIN deposits d ON d.id = dsj.deposit_id
        JOIN wallet_addresses wa ON wa.id = d.wallet_address_id
        WHERE NOT EXISTS (
            SELECT 1 FROM unified_sweep_jobs usj 
            WHERE usj.source = 'persistent' AND usj.source_id = dsj.deposit_id
        )
    """))
    
    # === 3. Удаляем старые таблицы ===
    op.drop_table('sweep_jobs')
    op.drop_table('deposit_sweep_jobs')


def downgrade() -> None:
    # Восстанавливаем таблицы (без данных)
    op.create_table(
        'sweep_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('payment_session_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('state', postgresql.ENUM('pending_gas', 'funding', 'sweeping', 'completed', 'failed', name='sweep_state', create_type=False), nullable=False),
        sa.Column('gas_tx_hash', sa.String(66)),
        sa.Column('sweep_tx_hash', sa.String(66)),
        sa.Column('attempts', sa.Integer, nullable=False),
        sa.Column('max_attempts', sa.Integer, nullable=False),
        sa.Column('last_error', sa.Text),
        sa.Column('next_retry_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['payment_session_id'], ['payment_sessions.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('payment_session_id'),
    )
    op.create_index('idx_sweep_state_retry', 'sweep_jobs', ['state', 'next_retry_at'])
    
    op.create_table(
        'deposit_sweep_jobs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('deposit_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('state', postgresql.ENUM('pending_gas', 'funding', 'sweeping', 'completed', 'failed', name='sweep_state', create_type=False), nullable=False),
        sa.Column('gas_tx_hash', sa.String(66)),
        sa.Column('sweep_tx_hash', sa.String(66)),
        sa.Column('attempts', sa.Integer, nullable=False),
        sa.Column('max_attempts', sa.Integer, nullable=False),
        sa.Column('last_error', sa.Text),
        sa.Column('next_retry_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['deposit_id'], ['deposits.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('deposit_id', name='uq_deposit_sweep_job_deposit'),
    )
    op.create_index('idx_deposit_sweep_state', 'deposit_sweep_jobs', ['state'])
    op.create_index('idx_deposit_sweep_state_retry', 'deposit_sweep_jobs', ['state', 'next_retry_at'])
