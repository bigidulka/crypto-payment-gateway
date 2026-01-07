"""
Экспорт всех моделей из одного места.
"""

from src.db.models.base import Base, TimestampMixin, UUIDMixin
from src.db.models.invoice import Invoice, InvoiceEvent, InvoiceStatus
from src.db.models.merchant import ApiKey, Merchant, Webhook
from src.db.models.payment import DepositAddress, OnchainTx, PaymentSession, TxStatus
from src.db.models.sweep import (
    ChainCheckpoint,
    OutboxStatus,
    OutboxWebhook,
    SweepJob,
    SweepState,
    SystemLog,
    SystemLogLevel,
)
from src.db.models.user_wallet import (
    Deposit,
    DepositStatus,
    UserBalance,
    UserWallet,
    WalletAddress,
)

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    "UUIDMixin",
    # Merchant
    "Merchant",
    "ApiKey",
    "Webhook",
    # Invoice
    "Invoice",
    "InvoiceEvent",
    "InvoiceStatus",
    # Payment
    "DepositAddress",
    "PaymentSession",
    "OnchainTx",
    "TxStatus",
    # Sweep
    "SweepJob",
    "SweepState",
    "OutboxWebhook",
    "OutboxStatus",
    "ChainCheckpoint",
    # Logs
    "SystemLog",
    "SystemLogLevel",
    # User Wallets (Persistent Deposits)
    "UserWallet",
    "WalletAddress",
    "Deposit",
    "DepositStatus",
    "UserBalance",
]
