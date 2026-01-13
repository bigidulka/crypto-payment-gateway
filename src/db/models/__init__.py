"""
Экспорт всех моделей из одного места.
"""

from src.db.models.base import Base, TimestampMixin, UUIDMixin

# Все enum'ы из центрального места
from src.db.models.enums import (
    DepositStatus,
    InvoiceStatus,
    OutboxStatus,
    SweepSource,
    SweepState,
    SystemLogLevel,
    TxStatus,
    enum_values,
)
from src.db.models.invoice import Invoice, InvoiceEvent
from src.db.models.merchant import ApiKey, Merchant, Webhook
from src.db.models.payment import DepositAddress, OnchainTx, PaymentSession
from src.db.models.sweep import (
    ChainCheckpoint,
    OutboxWebhook,
    SystemLog,
)
from src.db.models.unified_sweep import UnifiedSweepJob
from src.db.models.user_wallet import (
    Deposit,
    UserBalance,
    UserWallet,
    WalletAddress,
)

__all__ = [
    # Base
    "Base",
    "TimestampMixin",
    "UUIDMixin",
    # Enums (все из enums.py)
    "InvoiceStatus",
    "TxStatus",
    "SweepState",
    "SweepSource",
    "DepositStatus",
    "OutboxStatus",
    "SystemLogLevel",
    "enum_values",
    # Merchant
    "Merchant",
    "ApiKey",
    "Webhook",
    # Invoice
    "Invoice",
    "InvoiceEvent",
    # Payment
    "DepositAddress",
    "PaymentSession",
    "OnchainTx",
    # Sweep (unified - single table for all sweep jobs)
    "UnifiedSweepJob",
    # Webhooks
    "OutboxWebhook",
    "ChainCheckpoint",
    # Logs
    "SystemLog",
    # User Wallets (Persistent Deposits)
    "UserWallet",
    "WalletAddress",
    "Deposit",
    "UserBalance",
]
