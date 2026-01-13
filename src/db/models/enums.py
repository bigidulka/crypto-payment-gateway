"""
Центральное хранилище всех Enum типов для моделей БД.

Все enum'ы используют str mixin для корректной сериализации в PostgreSQL.
ВАЖНО: Значения enum (value) должны совпадать с типами в PostgreSQL.

ИСПОЛЬЗОВАНИЕ:
    # В моделях используйте values_callable для корректной сериализации:
    from src.db.models.enums import SweepState, enum_values
    
    state: Mapped[SweepState] = mapped_column(
        Enum(SweepState, name="sweep_state", values_callable=enum_values(SweepState)),
        default=SweepState.PENDING_GAS,
    )

ВАЖНО:
    Без values_callable SQLAlchemy будет использовать .name (PENDING_GAS)
    вместо .value (pending_gas), что приведёт к ошибке:
    "invalid input value for enum sweep_state: 'PENDING_GAS'"
"""

import enum


class InvoiceStatus(str, enum.Enum):
    """Статусы инвойса."""

    CREATED = "CREATED"  # Создан, ожидает выбора сети
    AWAITING_PAYMENT = "AWAITING_PAYMENT"  # Сеть выбрана, ожидает оплаты
    SEEN_ONCHAIN = "SEEN_ONCHAIN"  # Транзакция найдена, но ещё не подтверждена
    CONFIRMED = "CONFIRMED"  # Платёж подтверждён
    EXPIRED = "EXPIRED"  # Время истекло


class TxStatus(str, enum.Enum):
    """Статусы транзакции."""

    PENDING = "pending"  # Ожидает подтверждений
    CONFIRMING = "confirming"  # Набирает подтверждения
    CONFIRMED = "confirmed"  # Подтверждена


class SweepState(str, enum.Enum):
    """
    Состояние sweep job.
    
    ВАЖНО: Значения должны быть в нижнем регистре для соответствия
    PostgreSQL enum типу 'sweep_state'.
    """

    PENDING_GAS = "pending_gas"  # Ожидает проверки газа
    FUNDING = "funding"  # Отправка газа на deposit address
    SWEEPING = "sweeping"  # Вывод токенов на treasury
    COMPLETED = "completed"  # Успешно завершено
    FAILED = "failed"  # Ошибка


class SweepSource(str, enum.Enum):
    """
    Источник sweep job.
    
    ВАЖНО: Значения должны быть в нижнем регистре для PostgreSQL.
    """

    INVOICE = "invoice"  # Оплата инвойса (poller)
    PERSISTENT = "persistent"  # Пополнение persistent адреса (persistent_poller)
    MANUAL = "manual"  # Ручной sweep через API


class DepositStatus(str, enum.Enum):
    """
    Статус депозита.
    
    ВАЖНО: Значения должны быть в нижнем регистре для соответствия
    PostgreSQL enum типу 'deposit_status'.
    """

    PENDING = "pending"  # Обнаружен, ждём подтверждений
    CONFIRMING = "confirming"  # Набираем подтверждения
    CONFIRMED = "confirmed"  # Подтверждён, зачислен на баланс
    SWEPT = "swept"  # Средства переведены в treasury


class OutboxStatus(str, enum.Enum):
    """
    Статус webhook в outbox.
    
    ВАЖНО: Значения должны быть в нижнем регистре для соответствия
    PostgreSQL enum типу 'outbox_status'.
    """

    PENDING = "pending"  # Ожидает отправки
    SENT = "sent"  # Успешно отправлен
    FAILED = "failed"  # Все попытки исчерпаны


class SystemLogLevel(str, enum.Enum):
    """Уровни логирования системных событий."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# =============================================================================
# Helper функция для SQLAlchemy Enum
# =============================================================================

def enum_values(enum_class):
    """
    Возвращает функцию для получения значений enum.
    Используется в SQLAlchemy Enum(values_callable=enum_values(MyEnum)).
    
    Это гарантирует, что SQLAlchemy будет использовать .value (например 'pending_gas')
    а не .name (например 'PENDING_GAS').
    """
    return lambda x: [e.value for e in x]
