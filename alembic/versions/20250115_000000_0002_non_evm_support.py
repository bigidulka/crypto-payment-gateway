"""Extend deposit_address for non-EVM chains

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20250115_000000_0002_non_evm_support"
down_revision: Union[str, None] = "20251225_000000_0001_initial_migration"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Расширяем поле address для non-EVM адресов."""
    # Увеличиваем размер поля address с 42 до 70 символов
    # для поддержки Solana (32-44) и TON (48-66) адресов
    with op.batch_alter_table("deposit_addresses") as batch_op:
        batch_op.alter_column(
            "address",
            existing_type=sa.String(42),
            type_=sa.String(70),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Откат — ТОЛЬКО если нет non-EVM адресов!"""
    # ВНИМАНИЕ: Откат может обрезать данные!
    with op.batch_alter_table("deposit_addresses") as batch_op:
        batch_op.alter_column(
            "address",
            existing_type=sa.String(70),
            type_=sa.String(42),
            existing_nullable=False,
        )
