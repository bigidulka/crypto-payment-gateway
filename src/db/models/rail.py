"""
Модель рельса: конкретный платёжный провайдер, подключённый мерчантом.

Рельс - это EVM-цепочка (общая, без кред), кастодиальный API (CryptoBot,
xRocket, TG Wallet - креды мерчанта) или иная сеть (Solana/TON - общая
инфраструктура шлюза).
"""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.models.base import (
    Base,
    TimestampMixin,
    UniversalArray,
    UniversalUUID,
    UUIDMixin,
)



class Rail(Base, UUIDMixin, TimestampMixin):
    """
    Подключённый платёжный рельс.

    credentials хранит JSON (токены API, идентификаторы приложений),
    зашифрованный AES-256-GCM ключом ENCRYPTION_KEY. В открытом виде креды
    существуют только в памяти процесса на время вызова.
    """

    __tablename__ = "rails"

    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UniversalUUID(),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # evm | solana | ton | cryptobot | xrocket | tgwallet
    rail_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Для on-chain рельсов - идентификатор сети из chains.toml (bsc, base...).
    # Для кастодиальных - регион/сеть провайдера, например MAIN_NET.
    network: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # JSON-строка, зашифрованная encrypt_secret(). None для общих рельсов.
    encrypted_credentials: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Активы, которые рельс принимает: ["USDT", "USDC"]
    assets: Mapped[List[str]] = mapped_column(
        UniversalArray(String(32)), nullable=False, default=list
    )

    # Человекочитаемое имя подключения ("Мой CryptoBot-магазин")
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Если у рельса есть собственный баланс (кастодиальные), сюда пишем
    # момент последней сверки, чтобы не дёргать API чаще нужного.
    last_balance_check_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    merchant = relationship("Merchant", backref="rails")
