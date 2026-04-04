import enum
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, ForeignKey, Enum, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.db.base import Base

class PaymentStatus(str, enum.Enum):
    PENDING = "pending"         # Ожидает оплаты
    DETECTED = "detected"       # Транзакция обнаружена в мемпуле/блоке
    CONFIRMED = "confirmed"     # Оплата подтверждена (достаточно подтверждений)
    GAS_SENT = "gas_sent"       # Отправлен газ для вывода
    COMPLETED = "completed"     # Средства переведены на мастер-кошелек
    EXPIRED = "expired"         # Время вышло
    FAILED = "failed"           # Ошибка

class ChainType(str, enum.Enum):
    BNB = "BNB"
    BASE = "BASE"
    ARBITRUM = "ARBITRUM"

class Check(Base):
    __tablename__ = "checks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False) # e.g., USDT, ETH, BNB
    chain = Column(Enum(ChainType), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    description = Column(String, nullable=True)
    
    # Связь с платежом (один к одному)
    payment = relationship("Payment", back_populates="check", uselist=False)

class Payment(Base):
    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    check_id = Column(UUID(as_uuid=True), ForeignKey("checks.id"), unique=True, nullable=False)
    
    # Временный кошелек для приема средств
    wallet_address = Column(String, nullable=False)
    wallet_private_key = Column(String, nullable=False) # В MVP храним так, в проде нужен KMS/Vault!
    
    status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    
    tx_hash_in = Column(String, nullable=True)  # Хеш входящей транзакции (от юзера)
    tx_hash_gas = Column(String, nullable=True) # Хеш транзакции пополнения газа
    tx_hash_out = Column(String, nullable=True) # Хеш транзакции вывода на мастер
    
    amount_received = Column(Float, default=0.0)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    check = relationship("Check", back_populates="payment")
