"""
База для всех моделей и общие миксины.
"""

import json
import uuid as uuid_module
from datetime import datetime
from typing import Any, List

from sqlalchemy import DateTime, LargeBinary, String, Text, TypeDecorator, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# ============================================================================
# Универсальные типы для совместимости PostgreSQL и SQLite
# ============================================================================


class UniversalBytes(TypeDecorator):
    """
    Тип для хранения байтов.
    - PostgreSQL: BYTEA
    - SQLite: BLOB (LargeBinary)
    """

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.BYTEA())
        return dialect.type_descriptor(LargeBinary())


class UniversalJSON(TypeDecorator):
    """
    Тип для хранения JSON.
    - PostgreSQL: JSONB (индексируемый)
    - SQLite: TEXT с JSON сериализацией
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value  # PostgreSQL обрабатывает JSONB сам
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value  # PostgreSQL возвращает dict напрямую
        if isinstance(value, str):
            return json.loads(value)
        return value


class UniversalArray(TypeDecorator):
    """
    Тип для хранения массива строк.
    - PostgreSQL: ARRAY(String)
    - SQLite: TEXT с JSON сериализацией
    """

    impl = Text
    cache_ok = True

    def __init__(self, item_type=String(50)):
        super().__init__()
        self.item_type = item_type

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.ARRAY(self.item_type))
        return dialect.type_descriptor(Text())

    def process_bind_param(
        self, value: List[str] | None, dialect: Dialect
    ) -> str | List[str] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value  # PostgreSQL обрабатывает ARRAY сам
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: Any, dialect: Dialect) -> List[str] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value  # PostgreSQL возвращает list напрямую
        if isinstance(value, str):
            return json.loads(value)
        return value


class UniversalUUID(TypeDecorator):
    """
    Тип для хранения UUID.
    - PostgreSQL: UUID (native)
    - SQLite: CHAR(36) (строка)
    """

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect: Dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        # SQLite: конвертируем в строку
        return str(value) if value else None

    def process_result_value(self, value, dialect: Dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        # SQLite: конвертируем строку обратно в UUID
        if isinstance(value, str):
            return uuid_module.UUID(value)
        return value


# ============================================================================
# Базовые классы и миксины
# ============================================================================


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""

    pass


class TimestampMixin:
    """Миксин для автоматических timestamp полей."""

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


class UUIDMixin:
    """Миксин для UUID первичного ключа."""

    id: Mapped[uuid_module.UUID] = mapped_column(
        UniversalUUID(),
        primary_key=True,
        default=uuid_module.uuid4,
    )
