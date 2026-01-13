"""Add deposit_sweep_jobs table for persistent deposits sweep tracking.

Revision ID: 0004_deposit_sweep_jobs
Revises: 0003_user_wallets
Create Date: 2026-01-09

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0004_deposit_sweep_jobs'
down_revision = '0003_user_wallets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use existing sweep_state enum (already created in sweep_jobs table migration)
    sweep_state_enum = postgresql.ENUM('pending_gas', 'funding', 'sweeping', 'completed', 'failed', name='sweep_state', create_type=False)
    
    # Create deposit_sweep_jobs table
    op.create_table(
        'deposit_sweep_jobs',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('deposit_id', sa.UUID(), nullable=False),
        sa.Column('state', sweep_state_enum, nullable=False),
        sa.Column('gas_tx_hash', sa.String(66), nullable=True),
        sa.Column('sweep_tx_hash', sa.String(66), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, default=0),
        sa.Column('max_attempts', sa.Integer(), nullable=False, default=10),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['deposit_id'], ['deposits.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('deposit_id', name='uq_deposit_sweep_job_deposit'),
    )
    
    # Create indexes
    op.create_index('idx_deposit_sweep_state', 'deposit_sweep_jobs', ['state'])
    op.create_index('idx_deposit_sweep_state_retry', 'deposit_sweep_jobs', ['state', 'next_retry_at'])


def downgrade() -> None:
    op.drop_index('idx_deposit_sweep_state_retry', table_name='deposit_sweep_jobs')
    op.drop_index('idx_deposit_sweep_state', table_name='deposit_sweep_jobs')
    op.drop_table('deposit_sweep_jobs')
