"""Signed transactions are persisted before broadcast, never rebuilt on retry."""

from dataclasses import dataclass, field
from typing import Literal


class ChainRPCError(RuntimeError):
    """Safe diagnostic: never contains a keyed URL, credentials or signed bytes."""


class ChainEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedTransfer:
    chain: str
    tx_id: str
    payload: str = field(repr=False)
    sender: str
    destination: str
    token: str
    amount_raw: int
    fee_budget_raw: int
    context: dict


@dataclass(frozen=True)
class TransferReceipt:
    status: Literal["completed", "failed", "pending", "unknown", "reconciliation_required"]
    tx_id: str
    fee_raw: int | None = None
    reason: str | None = None
    wallet_released: bool = False
    native_cost_raw: int | None = None
