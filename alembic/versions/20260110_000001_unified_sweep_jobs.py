"""Add unified_sweep_jobs table for centralized sweep queue.

Revision ID: 0005_unified_sweep
Revises: 0004_deposit_sweep_jobs
Create Date: 2026-01-10

Создаёт единую таблицу для всех sweep операций.
Все сервисы (poller, persistent_poller) создают задачи здесь,
а единый Batch Sweeper их обрабатывает.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0005_unified_sweep'
down_revision = '0004_deposit_sweep_jobs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create sweep_source enum
    sweep_source = postgresql.ENUM(
        'invoice', 'persistent', 'manual',
        name='sweep_source',
        create_type=False,
    )
    sweep_source.create(op.get_bind(), checkfirst=True)
    
    # Create unified_sweep_jobs table
    op.create_table(
        'unified_sweep_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        
        # Source info
        sa.Column('source', sweep_source, nullable=False),
        sa.Column('source_id', sa.UUID(), nullable=False),
        
        # Blockchain info
        sa.Column('chain', sa.String(32), nullable=False),
        sa.Column('token', sa.String(10), nullable=False),
        sa.Column('token_contract', sa.String(66), nullable=False),
        
        # Wallet info
        sa.Column('from_address', sa.String(66), nullable=False),
        sa.Column('to_address', sa.String(66), nullable=False),
        sa.Column('encrypted_private_key', sa.String(512), nullable=False),
        
        # Amount
        sa.Column('amount', sa.Numeric(36, 18), nullable=False),
        sa.Column('amount_raw', sa.String(78), nullable=False),
        
        # State (reuse existing sweep_state enum)
        sa.Column('state', postgresql.ENUM(
            'pending_gas', 'funding', 'sweeping', 'completed', 'failed',
            name='sweep_state', create_type=False
        ), nullable=False, server_default='pending_gas'),
        
        # Transaction hashes
        sa.Column('gas_tx_hash', sa.String(66), nullable=True),
        sa.Column('sweep_tx_hash', sa.String(66), nullable=True),
        
        # Gas info
        sa.Column('estimated_gas_wei', sa.String(78), nullable=True),
        sa.Column('native_balance_wei', sa.String(78), nullable=True),
        sa.Column('needs_gas_funding', sa.Boolean(), nullable=False, server_default='true'),
        
        # Retry logic
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_attempts', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
        
        # Priority
        sa.Column('priority', sa.Integer(), nullable=False, server_default='0'),
        
        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        
        sa.PrimaryKeyConstraint('id'),
    )
    
    # Create indexes
    op.create_index('idx_unified_sweep_source', 'unified_sweep_jobs', ['source'])
    op.create_index('idx_unified_sweep_source_id', 'unified_sweep_jobs', ['source_id'])
    op.create_index('idx_unified_sweep_chain', 'unified_sweep_jobs', ['chain'])
    op.create_index('idx_unified_sweep_from_address', 'unified_sweep_jobs', ['from_address'])
    op.create_index('idx_unified_sweep_state', 'unified_sweep_jobs', ['state'])
    op.create_index('idx_unified_sweep_next_retry', 'unified_sweep_jobs', ['next_retry_at'])
    
    # Unique constraint: one job per source
    op.create_index(
        'uq_unified_sweep_source',
        'unified_sweep_jobs',
        ['source', 'source_id'],
        unique=True,
    )
    
    # Composite indexes for batch processing
    op.create_index(
        'idx_unified_sweep_pending',
        'unified_sweep_jobs',
        ['state', 'next_retry_at', 'priority'],
    )
    op.create_index(
        'idx_unified_sweep_chain_state',
        'unified_sweep_jobs',
        ['chain', 'state'],
    )


def downgrade() -> None:
    op.drop_index('idx_unified_sweep_chain_state', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_pending', table_name='unified_sweep_jobs')
    op.drop_index('uq_unified_sweep_source', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_next_retry', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_state', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_from_address', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_chain', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_source_id', table_name='unified_sweep_jobs')
    op.drop_index('idx_unified_sweep_source', table_name='unified_sweep_jobs')
    op.drop_table('unified_sweep_jobs')
    
    # Drop enum
    op.execute("DROP TYPE IF EXISTS sweep_source")
