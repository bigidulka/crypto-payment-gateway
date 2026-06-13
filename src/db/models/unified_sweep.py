"""
Unified Sweep Job - единая модель для всех sweep операций.

Все сервисы (poller, persistent_poller) создают UnifiedSweepJob,
а единый Batch Sweeper их обрабатывает.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base
from src.db.models.enums import SweepSource, SweepState, enum_values


class UnifiedSweepJob(Base):
    """
    Единая задача на вывод токенов.
    
    Workflow:
    1. Сервис (poller/persistent_poller) создаёт job со state=PENDING
    2. Batch Sweeper берёт PENDING jobs, отправляет газ → FUNDING
    3. После подтверждения газа → SWEEPING
    4. После sweep → COMPLETED или FAILED
    
    Все данные для sweep хранятся в job - не нужны JOIN'ы.
    """

    __tablename__ = "unified_sweep_jobs"

    # === Primary Key ===
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # === Source Info ===
    source: Mapped[SweepSource] = mapped_column(
        Enum(SweepSource, name="sweep_source", values_callable=enum_values(SweepSource)),
        nullable=False,
        index=True,
    )
    
    # ID источника (payment_session_id или deposit_id)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # === Blockchain Info ===
    chain: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(10), nullable=False)  # USDT, USDC
    token_contract: Mapped[str] = mapped_column(String(66), nullable=False)
    
    # === Wallet Info ===
    from_address: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    to_address: Mapped[str] = mapped_column(String(66), nullable=False)  # treasury
    
    # Зашифрованный приватный ключ (берём при создании job)
    encrypted_private_key: Mapped[str] = mapped_column(String(512), nullable=False)
    
    # === Amount ===
    amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    amount_raw: Mapped[str] = mapped_column(String(78), nullable=False)  # BigInt as string

    # === State ===
    state: Mapped[SweepState] = mapped_column(
        Enum(SweepState, name="sweep_state", values_callable=enum_values(SweepState), create_constraint=False),
        default=SweepState.PENDING_GAS,
        nullable=False,
        index=True,
    )

    # === Transaction Hashes ===
    gas_tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)
    sweep_tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)

    # === Gas Info (для оптимизации) ===
    estimated_gas_wei: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    native_balance_wei: Mapped[Optional[str]] = mapped_column(String(78), nullable=True)
    needs_gas_funding: Mapped[bool] = mapped_column(default=True, nullable=False)

    # === Retry Logic ===
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # === Priority ===
    # Больший amount = больший приоритет
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)

    # === Timestamps ===
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # Уникальность: один source_id = один job
        Index("uq_unified_sweep_source", "source", "source_id", unique=True),
        # Для быстрого поиска pending jobs
        Index("idx_unified_sweep_pending", "state", "next_retry_at", "priority"),
        # Для batch обработки по сетям
        Index("idx_unified_sweep_chain_state", "chain", "state"),
    )

    def __repr__(self) -> str:
        return (
            f"<UnifiedSweepJob {self.id} "
            f"chain={self.chain} state={self.state.value} "
            f"amount={self.amount} {self.token}>"
        )
