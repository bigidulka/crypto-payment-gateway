"""
Модель рельса: конкретный платёжный провайдер, подключённый мерчантом.

Рельс - это EVM-цепочка (общая, без кред), кастодиальный API (CryptoBot,
xRocket, TG Wallet - креды мерчанта) или иная сеть (Solana/TON - общая
инфраструктура шлюза).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, event
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship, validates

from src.db.models.base import (
    Base,
    TimestampMixin,
    UniversalJSON,
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
    network: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # JSON-строка, зашифрованная encrypt_secret(). None для общих рельсов.
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Migration 0009 created PostgreSQL JSON, not ARRAY/JSONB. MutableList
    # tracks in-place changes; validation keeps the list[str] contract.
    assets: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(UniversalJSON(postgres_jsonb=False)), nullable=False, default=list
    )

    # Человекочитаемое имя подключения ("Мой CryptoBot-магазин")
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @staticmethod
    def _canonical_assets(assets: object) -> list[str]:
        """Require a non-empty list of canonical, unique asset symbols."""
        if not isinstance(assets, list) or not assets:
            raise ValueError("assets must be a non-empty list of asset symbols")

        normalized: list[str] = []
        for asset in assets:
            if not isinstance(asset, str):
                raise ValueError("assets must contain only strings")
            symbol = asset.strip().upper()
            if not symbol:
                raise ValueError("assets cannot contain empty symbols")
            if symbol != asset:
                raise ValueError("asset symbols must be trimmed uppercase")
            if symbol in normalized:
                raise ValueError("asset symbols must not contain duplicates")
            normalized.append(symbol)
        return normalized

    @validates("assets")
    def validate_assets(self, _key: str, assets: object) -> list[str]:
        """Validate assignments before the mutable JSON list is attached."""
        return self._canonical_assets(assets)

    # Если у рельса есть собственный баланс (кастодиальные), сюда пишем
    # момент последней сверки, чтобы не дёргать API чаще нужного.
    last_balance_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    merchant = relationship("Merchant", backref="rails")


@event.listens_for(Rail, "before_insert")
@event.listens_for(Rail, "before_update")
def validate_persisted_rail_assets(_mapper: Mapper, _connection, target: Rail) -> None:
    """Catch omitted defaults and in-place MutableList mutations at flush time."""
    target._canonical_assets(target.assets)
