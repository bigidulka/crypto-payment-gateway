"""Atomic ledger posting primitives for the disabled ledger foundation.

No route, invoice, scanner, rail, or worker calls this service yet. The
caller-owned API permits future invoice/outbox composition without this module
committing or rolling back caller work.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.ledger import (
    LedgerAccount,
    LedgerDirection,
    LedgerEntry,
    LedgerTransaction,
    LedgerTransactionStatus,
)
from src.ledger.amounts import ATOMIC_AMOUNT_UPPER_BOUND


class LedgerValidationError(ValueError):
    pass


class LedgerIdempotencyConflict(LedgerValidationError):
    pass


class LedgerSourceConflict(LedgerValidationError):
    """External source coordinate is reserved; deliberately contains no owner data."""


@dataclass(frozen=True)
class LedgerLine:
    account_id: uuid.UUID
    asset_id: uuid.UUID
    direction: LedgerDirection
    amount_atomic: int | Decimal


@dataclass(frozen=True)
class _NormalizedLine:
    account_id: uuid.UUID
    asset_id: uuid.UUID
    direction: LedgerDirection
    amount_atomic: int


def _atomic(value: int | Decimal) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise LedgerValidationError("atomic amount must be an int or Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerValidationError("atomic amount must be finite")
        if not Decimal(0) < value < Decimal(ATOMIC_AMOUNT_UPPER_BOUND):
            raise LedgerValidationError("atomic amount must be in range 1..10**78-1")
        integral = value.to_integral_value()
        if integral != value:
            raise LedgerValidationError("atomic amount must be an integer")
        return int(integral)
    if not 0 < value < ATOMIC_AMOUNT_UPPER_BOUND:
        raise LedgerValidationError("atomic amount must be in range 1..10**78-1")
    return value


def _required_identity(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise LedgerValidationError(f"{name} must be a non-blank string up to {maximum} characters")
    return value


def _digest(
    source_namespace: str,
    source_external_id: str,
    source_digest: str,
    lines: tuple[_NormalizedLine, ...],
) -> str:
    canonical_lines = sorted(
        (
            {
                "account_id": str(line.account_id),
                "asset_id": str(line.asset_id),
                "direction": line.direction.value,
                "amount_atomic": str(line.amount_atomic),
            }
            for line in lines
        ),
        key=lambda line: (
            line["asset_id"],
            line["account_id"],
            line["direction"],
            line["amount_atomic"],
        ),
    )
    payload = {
        "source_namespace": source_namespace,
        "source_external_id": source_external_id,
        "source_digest": source_digest,
        "lines": canonical_lines,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _database_constraint_name(exc: IntegrityError) -> str | None:
    """Find asyncpg metadata through SQLAlchemy wrappers without parsing text."""
    seen: set[int] = set()
    pending: list[object] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        name = getattr(current, "constraint_name", None)
        if isinstance(name, str):
            return name
        for attribute in ("orig", "__cause__", "__context__"):
            nested = getattr(current, attribute, None)
            if nested is not None:
                pending.append(nested)
    return None


class LedgerPostingService:
    def __init__(self, session: AsyncSession):
        self.session = session

    def _normalize(
        self,
        *,
        merchant_id: uuid.UUID,
        source_namespace: str,
        source_external_id: str,
        source_digest: str,
        idempotency_key: str,
        lines: Iterable[LedgerLine],
    ):
        if not isinstance(merchant_id, uuid.UUID):
            raise LedgerValidationError("merchant_id must be a UUID")
        namespace = _required_identity(source_namespace, "source_namespace", 128)
        external_id = _required_identity(source_external_id, "source_external_id", 512)
        digest = _required_identity(source_digest, "source_digest", 64)
        key = _required_identity(idempotency_key, "idempotency_key", 128)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise LedgerValidationError("source_digest must be a lowercase SHA-256 hex digest")
        normalized: list[_NormalizedLine] = []
        for line in lines:
            if (
                not isinstance(line, LedgerLine)
                or not isinstance(line.account_id, uuid.UUID)
                or not isinstance(line.asset_id, uuid.UUID)
            ):
                raise LedgerValidationError(
                    "ledger lines require UUID account and asset identifiers"
                )
            if not isinstance(line.direction, LedgerDirection):
                raise LedgerValidationError("ledger line direction must be LedgerDirection")
            normalized.append(
                _NormalizedLine(
                    line.account_id, line.asset_id, line.direction, _atomic(line.amount_atomic)
                )
            )
        if len(normalized) < 2:
            raise LedgerValidationError("ledger posting requires at least two entries")
        per_asset: dict[uuid.UUID, int] = {}
        for line in normalized:
            sign = 1 if line.direction is LedgerDirection.DEBIT else -1
            per_asset[line.asset_id] = per_asset.get(line.asset_id, 0) + sign * line.amount_atomic
        if any(total != 0 for total in per_asset.values()):
            raise LedgerValidationError("ledger entries must balance per asset")
        normalized_lines = tuple(normalized)
        return (
            namespace,
            external_id,
            digest,
            key,
            normalized_lines,
            _digest(namespace, external_id, digest, normalized_lines),
        )

    async def post_in_transaction(self, **kwargs) -> LedgerTransaction:
        """Post inside an active caller transaction; never commit/rollback caller work.

        SQLAlchemy begins a savepoint with a pre-flush; callers should deliberately
        flush pending unrelated writes first if they require different ordering.
        """
        if not self.session.in_transaction():
            raise LedgerValidationError("post_in_transaction requires an active caller transaction")
        merchant_id = kwargs["merchant_id"]
        namespace, external_id, digest, key, normalized, posting_digest = self._normalize(**kwargs)
        try:
            async with self.session.begin_nested():
                existing = await self.session.scalar(
                    select(LedgerTransaction)
                    .where(
                        LedgerTransaction.merchant_id == merchant_id,
                        LedgerTransaction.idempotency_key == key,
                    )
                    .with_for_update()
                )
                if existing:
                    if existing.posting_digest == posting_digest:
                        return existing
                    raise LedgerIdempotencyConflict("idempotency key reused with different posting")
                account_ids = {line.account_id for line in normalized}
                accounts = (
                    await self.session.scalars(
                        select(LedgerAccount).where(
                            LedgerAccount.merchant_id == merchant_id,
                            LedgerAccount.id.in_(account_ids),
                        )
                    )
                ).all()
                if len(accounts) != len(account_ids):
                    raise LedgerValidationError("ledger account does not belong to merchant")
                transaction = LedgerTransaction(
                    merchant_id=merchant_id,
                    status=LedgerTransactionStatus.OPEN.value,
                    source_namespace=namespace,
                    source_external_id=external_id,
                    source_digest=digest,
                    idempotency_key=key,
                    posting_digest=posting_digest,
                )
                self.session.add(transaction)
                await self.session.flush()
                self.session.add_all(
                    LedgerEntry(
                        merchant_id=merchant_id,
                        ledger_transaction_id=transaction.id,
                        ledger_account_id=line.account_id,
                        ledger_asset_id=line.asset_id,
                        direction=line.direction.value,
                        amount_atomic=line.amount_atomic,
                    )
                    for line in normalized
                )
                await self.session.flush()
                transaction.status = LedgerTransactionStatus.POSTED.value
                transaction.posted_at = datetime.now(UTC)
                await self.session.flush()
                return transaction
        except IntegrityError as exc:
            constraint_name = _database_constraint_name(exc)
            if constraint_name not in {
                "uq_ledger_transaction_idempotency",
                "uq_ledger_transaction_source",
            }:
                raise
            # Either unique index can win for simultaneous identical submissions.
            # Read only the caller's own merchant/key, never a global source owner.
            existing = await self.session.scalar(
                select(LedgerTransaction).where(
                    LedgerTransaction.merchant_id == merchant_id,
                    LedgerTransaction.idempotency_key == key,
                )
            )
            if existing:
                if existing.posting_digest == posting_digest:
                    return existing
                raise LedgerIdempotencyConflict(
                    "idempotency key reused with different posting"
                ) from exc
            if constraint_name == "uq_ledger_transaction_source":
                raise LedgerSourceConflict(
                    "external source coordinate is already recorded"
                ) from exc
            raise

    async def post(self, **kwargs) -> LedgerTransaction:
        """Standalone wrapper; active callers must explicitly use delegated API."""
        if self.session.in_transaction():
            raise LedgerValidationError("active caller transaction: use post_in_transaction")
        async with self.session.begin():
            return await self.post_in_transaction(**kwargs)
