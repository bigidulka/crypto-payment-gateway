"""Disabled internal ingestion of a server-verified custodial provider payment.

No route, worker, scanner, provider callback, invoice creation, payout, or
provider write calls this module. Verification is internal-only and its adapter
binding is supplied by trusted server-side construction when future wiring is
approved; injected bindings exist only to exercise this disabled slice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.db.models import (
    Invoice,
    InvoiceEvent,
    InvoiceStatus,
    LedgerAsset,
    OutboxStatus,
    OutboxWebhook,
    ProviderPaymentMapping,
    ProviderPaymentObservation,
    ProviderPaymentReceipt,
    ProviderPaymentRecipientIntent,
    ProviderVerificationDecision,
    Rail,
    Webhook,
)
from src.db.models.ledger import LedgerDirection, LedgerEntry, LedgerTransaction
from src.ledger.amounts import ATOMIC_AMOUNT_UPPER_BOUND, decimal_to_atomic
from src.payments.rails.base import RailPayment
from src.services.ledger_posting_service import LedgerLine, LedgerPostingService


class ProviderIngestionError(ValueError):
    """Safe internal rejection; it never includes credentials or raw provider data."""


class ProviderIngestionDisabled(ProviderIngestionError):
    pass


class ProviderReconciliationConflict(ProviderIngestionError):
    """A claimed payment identity conflicts with immutable economic bindings."""


@dataclass(frozen=True)
class ProviderVerificationBinding:
    """Trusted server-side identity of the configured provider application."""

    merchant_id: uuid.UUID
    mapping_id: uuid.UUID
    rail_id: uuid.UUID
    provider_account_identity: str
    provider_network: str
    ledger_asset_id: uuid.UUID


class ProviderPaymentVerifier(Protocol):
    """Internal adapter protocol. It must not be created from request data."""

    binding: ProviderVerificationBinding

    async def verify_payment(self, provider_invoice_id: str) -> RailPayment | None: ...


@dataclass(frozen=True)
class VerifiedProviderObservation:
    """Provider result bound to one merchant/mapping before it may be consumed."""

    merchant_id: uuid.UUID
    mapping_id: uuid.UUID
    rail_id: uuid.UUID
    provider_account_identity: str
    provider_network: str
    ledger_asset_id: uuid.UUID
    provider_invoice_id: str
    provider_asset_code: str
    observed_amount_atomic: int
    provider_paid_at: str
    source_kind: str
    economic_digest: str
    evidence_digest: str


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


async def _outbox_snapshot_digest(session: AsyncSession, outbox_id: uuid.UUID) -> str:
    """Hash persisted PostgreSQL JSONB outbox payload exactly as the DB guard does."""
    digest = await session.scalar(
        text(
            "SELECT encode(public.digest(convert_to(ob.payload::jsonb::text, 'UTF8'), 'sha256'::text), 'hex') "
            "FROM public.outbox_webhooks ob WHERE ob.id=:outbox_id"
        ),
        {"outbox_id": outbox_id},
    )
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProviderIngestionError("database did not return persisted outbox payload digest")
    return digest


def _uuid(value: object, label: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ProviderIngestionError(f"{label} must be a UUID")
    return value


def _nonblank(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ProviderIngestionError(
            f"{label} must be a non-blank string up to {maximum} characters"
        )
    return value


def _optional_timestamp(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 128:
        raise ProviderIngestionError(
            "provider paid timestamp must be a trimmed string up to 128 characters"
        )
    return value


def _atomic(amount: object, decimals: object) -> int:
    try:
        return decimal_to_atomic(amount, decimals)
    except ValueError as exc:
        raise ProviderIngestionError(str(exc)) from exc


def _atomic_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderIngestionError(f"{label} must be an int")
    if not 0 < value < ATOMIC_AMOUNT_UPPER_BOUND:
        raise ProviderIngestionError(f"{label} must be in range 1..10**78-1")
    return value


def _db_atomic(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ProviderIngestionError(f"{label} must be an exact atomic number")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ProviderIngestionError(f"{label} must be an exact atomic number")
        value = int(value)
    return _atomic_int(value, label)


def _digest_hex(value: object, label: str) -> str:
    value = _nonblank(value, label, 64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProviderIngestionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _binding_values(binding: ProviderVerificationBinding) -> ProviderVerificationBinding:
    if not isinstance(binding, ProviderVerificationBinding):
        raise ProviderIngestionError("verifier must expose a trusted provider verification binding")
    return ProviderVerificationBinding(
        merchant_id=_uuid(binding.merchant_id, "binding merchant id"),
        mapping_id=_uuid(binding.mapping_id, "binding mapping id"),
        rail_id=_uuid(binding.rail_id, "binding rail id"),
        provider_account_identity=_digest_hex(
            binding.provider_account_identity, "binding provider account identity"
        ),
        provider_network=_nonblank(binding.provider_network, "binding provider network", 64),
        ledger_asset_id=_uuid(binding.ledger_asset_id, "binding ledger asset id"),
    )


def _economic_digest(
    binding: ProviderVerificationBinding,
    provider_invoice_id: str,
    provider_asset_code: str,
    amount_atomic: int,
) -> str:
    """Stable economic identity; provider timestamps are noncanonical audit metadata here."""
    return _digest(
        {
            "v": 1,
            "provider_account_identity": binding.provider_account_identity,
            "provider_network": binding.provider_network,
            "provider_invoice_id": provider_invoice_id,
            "ledger_asset_id": str(binding.ledger_asset_id),
            "provider_asset_code": provider_asset_code,
            "amount_atomic": str(amount_atomic),
        }
    )


def _redacted_evidence(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if any(
                secret in str(key).lower()
                for secret in (
                    "token",
                    "secret",
                    "password",
                    "credential",
                    "authorization",
                    "api_key",
                )
            )
            else _redacted_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redacted_evidence(item) for item in value]
    return value


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


async def verify_cryptobot_payment(
    binding: ProviderVerificationBinding,
    provider_invoice_id: str,
    provider_asset_code: str,
    atomic_decimals: int,
    verifier: ProviderPaymentVerifier,
) -> VerifiedProviderObservation | None:
    """Call a trusted adapter outside SQL transaction and bind the successful result."""
    payment = await verifier.verify_payment(provider_invoice_id)
    if payment is None:
        return None
    payment_invoice_id = _nonblank(payment.provider_invoice_id, "provider invoice id", 256)
    payment_asset = _nonblank(payment.asset, "provider asset code", 32).upper()
    if payment_invoice_id != provider_invoice_id:
        raise ProviderReconciliationConflict("provider invoice identity mismatch")
    if payment_asset != provider_asset_code:
        raise ProviderReconciliationConflict("provider asset code mismatch")
    observed_atomic = _atomic(payment.amount, atomic_decimals)
    paid_at = _optional_timestamp(payment.paid_at)
    raw = payment.raw if isinstance(payment.raw, dict) else {}
    economic_digest = _economic_digest(binding, payment_invoice_id, payment_asset, observed_atomic)
    return VerifiedProviderObservation(
        merchant_id=binding.merchant_id,
        mapping_id=binding.mapping_id,
        rail_id=binding.rail_id,
        provider_account_identity=binding.provider_account_identity,
        provider_network=binding.provider_network,
        ledger_asset_id=binding.ledger_asset_id,
        provider_invoice_id=payment_invoice_id,
        provider_asset_code=payment_asset,
        observed_amount_atomic=observed_atomic,
        provider_paid_at=paid_at,
        source_kind="provider_poll",
        economic_digest=economic_digest,
        evidence_digest=_digest(_redacted_evidence(raw)),
    )


class ProviderPaymentIngestionService:
    """Disabled, internal-only atomic custodial observation consumption."""

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = _settings(settings)

    def _enabled_for(self, actor_merchant_id: object) -> uuid.UUID:
        merchant_id = _uuid(actor_merchant_id, "actor merchant id")
        if not self.settings.ledger_rail_orchestration_enabled:
            raise ProviderIngestionDisabled("provider ingestion is disabled")
        if str(merchant_id) not in self.settings.ledger_rail_orchestration_merchant_ids:
            raise ProviderIngestionDisabled("merchant is not allowlisted for provider ingestion")
        return merchant_id

    async def verify_provider_invoice(
        self,
        actor_merchant_id: uuid.UUID,
        mapping_id: uuid.UUID,
        verifier: ProviderPaymentVerifier,
    ) -> VerifiedProviderObservation | None:
        """Authorize before database/provider I/O, then verify after read cleanup."""
        actor_merchant_id = self._enabled_for(actor_merchant_id)
        mapping_id = _uuid(mapping_id, "mapping id")
        if self.session.in_transaction():
            raise ProviderIngestionError("provider verification requires no active transaction")
        try:
            mapping = await self.session.scalar(
                select(ProviderPaymentMapping).where(
                    ProviderPaymentMapping.id == mapping_id,
                    ProviderPaymentMapping.merchant_id == actor_merchant_id,
                )
            )
            if mapping is None:
                raise ProviderIngestionError("provider mapping not found")
            rail = await self.session.scalar(
                select(Rail).where(
                    Rail.id == mapping.rail_id, Rail.merchant_id == actor_merchant_id
                )
            )
            asset = await self.session.scalar(
                select(LedgerAsset).where(LedgerAsset.id == mapping.ledger_asset_id)
            )
            if rail is None or asset is None:
                raise ProviderIngestionError("provider mapping parent missing")
            network = _nonblank(mapping.provider_network, "mapping provider network", 64)
            binding = ProviderVerificationBinding(
                merchant_id=actor_merchant_id,
                mapping_id=mapping.id,
                rail_id=mapping.rail_id,
                provider_account_identity=_digest_hex(
                    mapping.provider_account_identity, "mapping provider account identity"
                ),
                provider_network=network,
                ledger_asset_id=mapping.ledger_asset_id,
            )
            if (
                not rail.is_active
                or rail.rail_type != "cryptobot"
                or rail.network != network
                or mapping.provider_asset_code not in rail.assets
                or asset.network_kind != "custodial"
                or asset.network_identifier != f"cryptobot:{network}"
                or asset.canonical_identifier != mapping.provider_asset_code
                or asset.is_native
            ):
                raise ProviderIngestionError(
                    "mapping no longer has an active trusted rail and asset binding"
                )
            if _binding_values(verifier.binding) != binding:
                raise ProviderReconciliationConflict(
                    "verifier binding does not match mapped trusted provider account"
                )
            invoice_id = _nonblank(mapping.provider_invoice_id, "mapping provider invoice id", 256)
            asset_code = _nonblank(mapping.provider_asset_code, "mapping provider asset code", 32)
            decimals = asset.atomic_decimals
        finally:
            # All read/error paths terminate before any external adapter I/O.
            if self.session.in_transaction():
                await self.session.rollback()
        return await verify_cryptobot_payment(binding, invoice_id, asset_code, decimals, verifier)

    async def consume_verified_observation(
        self,
        actor_merchant_id: uuid.UUID,
        observation: VerifiedProviderObservation,
    ) -> ProviderPaymentObservation:
        """Consume a fully bound result inside caller transaction without committing it."""
        actor_merchant_id = self._enabled_for(actor_merchant_id)
        self._validate_observation_envelope(actor_merchant_id, observation)
        if not self.session.in_transaction():
            raise ProviderIngestionError("caller-owned transaction required")
        async with self.session.begin_nested():
            return await self._consume_verified_observation(actor_merchant_id, observation)

    def _validate_observation_envelope(
        self, actor_merchant_id: uuid.UUID, observation: object
    ) -> None:
        if not isinstance(observation, VerifiedProviderObservation):
            raise ProviderIngestionError("verified provider observation has invalid type")
        if observation.merchant_id != actor_merchant_id:
            raise ProviderReconciliationConflict(
                "verified observation merchant does not match actor"
            )
        binding = _binding_values(
            ProviderVerificationBinding(
                merchant_id=observation.merchant_id,
                mapping_id=observation.mapping_id,
                rail_id=observation.rail_id,
                provider_account_identity=observation.provider_account_identity,
                provider_network=observation.provider_network,
                ledger_asset_id=observation.ledger_asset_id,
            )
        )
        provider_invoice_id = _nonblank(observation.provider_invoice_id, "provider invoice id", 256)
        provider_asset_code = _nonblank(observation.provider_asset_code, "provider asset code", 32)
        if provider_asset_code != provider_asset_code.upper():
            raise ProviderIngestionError("provider asset code must be uppercase")
        amount = _atomic_int(observation.observed_amount_atomic, "observed atomic amount")
        _optional_timestamp(observation.provider_paid_at)
        if observation.source_kind not in {"provider_poll", "provider_webhook"}:
            raise ProviderIngestionError("unsupported verified provider source")
        if _digest_hex(observation.economic_digest, "economic digest") != _economic_digest(
            binding, provider_invoice_id, provider_asset_code, amount
        ):
            raise ProviderReconciliationConflict(
                "verified observation economic digest does not match binding"
            )
        _digest_hex(observation.evidence_digest, "evidence digest")

    async def _assert_completed_projection(
        self,
        merchant_id: uuid.UUID,
        mapping: ProviderPaymentMapping,
        observation: VerifiedProviderObservation,
        fact: ProviderPaymentObservation,
    ) -> None:
        """Accept duplicates only when their original atomic projection is provable."""
        receipt = await self.session.scalar(
            select(ProviderPaymentReceipt)
            .where(
                ProviderPaymentReceipt.merchant_id == merchant_id,
                ProviderPaymentReceipt.observation_id == fact.id,
            )
            .with_for_update()
        )
        if receipt is None or receipt.invoice_id != mapping.invoice_id:
            raise ProviderReconciliationConflict(
                "existing provider observation lacks completed receipt proof"
            )
        decision = await self.session.scalar(
            select(ProviderVerificationDecision).where(
                ProviderVerificationDecision.id == receipt.decision_id,
                ProviderVerificationDecision.merchant_id == merchant_id,
                ProviderVerificationDecision.observation_id == fact.id,
                ProviderVerificationDecision.decision == "verified",
            )
        )
        transaction = await self.session.scalar(
            select(LedgerTransaction).where(
                LedgerTransaction.id == receipt.ledger_transaction_id,
                LedgerTransaction.merchant_id == merchant_id,
                LedgerTransaction.status == "posted",
                LedgerTransaction.source_namespace == "provider_payment_observation",
                LedgerTransaction.source_external_id == observation.economic_digest,
                LedgerTransaction.source_digest == observation.economic_digest,
            )
        )
        invoice = await self.session.scalar(
            select(Invoice).where(
                Invoice.id == receipt.invoice_id,
                Invoice.merchant_id == merchant_id,
                Invoice.status == InvoiceStatus.CONFIRMED,
            )
        )
        event = await self.session.scalar(
            select(InvoiceEvent).where(
                InvoiceEvent.id == receipt.invoice_event_id,
                InvoiceEvent.invoice_id == receipt.invoice_id,
                InvoiceEvent.event_id == receipt.event_id,
                InvoiceEvent.event_type == "invoice.confirmed",
            )
        )
        if None in (decision, transaction, invoice, event):
            raise ProviderReconciliationConflict(
                "existing provider observation has incomplete projection proof"
            )
        expected_payload = {
            "event": "invoice.confirmed",
            "event_id": receipt.event_id,
            "invoice": {
                "id": str(mapping.invoice_id),
                "public_id": invoice.public_id,
                "status": InvoiceStatus.CONFIRMED.value,
            },
            "observed": {
                "ledger_asset_id": str(mapping.ledger_asset_id),
                "network_kind": "custodial",
                "network_identifier": f"cryptobot:{mapping.provider_network}",
                "atomic_decimals": None,
                "provider_account_identity": mapping.provider_account_identity,
                "provider_network": mapping.provider_network,
                "asset_code": mapping.provider_asset_code,
                "amount_atomic": str(observation.observed_amount_atomic),
            },
        }
        asset = await self.session.scalar(
            select(LedgerAsset).where(LedgerAsset.id == mapping.ledger_asset_id)
        )
        if asset is None:
            raise ProviderReconciliationConflict(
                "existing provider observation asset proof missing"
            )
        expected_payload["observed"]["atomic_decimals"] = asset.atomic_decimals
        if event.payload != expected_payload:
            raise ProviderReconciliationConflict(
                "existing provider observation event payload proof mismatch"
            )
        entries = (
            await self.session.scalars(
                select(LedgerEntry).where(LedgerEntry.ledger_transaction_id == transaction.id)
            )
        ).all()
        required_lines = {
            (
                mapping.receivable_account_id,
                mapping.ledger_asset_id,
                "debit",
                observation.observed_amount_atomic,
            ),
            (
                mapping.payable_account_id,
                mapping.ledger_asset_id,
                "credit",
                observation.observed_amount_atomic,
            ),
        }
        actual_lines = {
            (
                entry.ledger_account_id,
                entry.ledger_asset_id,
                entry.direction,
                _db_atomic(entry.amount_atomic, "stored ledger atomic amount"),
            )
            for entry in entries
        }
        if len(entries) != 2 or actual_lines != required_lines:
            raise ProviderReconciliationConflict(
                "existing provider observation ledger proof mismatch"
            )
        intents = (
            await self.session.scalars(
                select(ProviderPaymentRecipientIntent).where(
                    ProviderPaymentRecipientIntent.receipt_id == receipt.id
                )
            )
        ).all()
        if len(intents) != receipt.intended_recipient_count:
            raise ProviderReconciliationConflict(
                "existing provider observation recipient intent proof mismatch"
            )
        if any(intent.payload_snapshot != expected_payload for intent in intents):
            raise ProviderReconciliationConflict(
                "existing provider observation recipient payload proof mismatch"
            )

    async def _consume_verified_observation(
        self,
        actor_merchant_id: uuid.UUID,
        observation: VerifiedProviderObservation,
    ) -> ProviderPaymentObservation:
        mapping = await self.session.scalar(
            select(ProviderPaymentMapping)
            .where(
                ProviderPaymentMapping.id == observation.mapping_id,
                ProviderPaymentMapping.merchant_id == actor_merchant_id,
            )
            .with_for_update()
        )
        if mapping is None:
            raise ProviderReconciliationConflict(
                "verified observation mapping does not belong to actor"
            )
        expected_atomic = _db_atomic(
            mapping.expected_amount_atomic, "mapped expected atomic amount"
        )
        if (
            observation.rail_id != mapping.rail_id
            or observation.provider_account_identity != mapping.provider_account_identity
            or observation.provider_network != mapping.provider_network
            or observation.ledger_asset_id != mapping.ledger_asset_id
            or observation.provider_invoice_id != mapping.provider_invoice_id
            or observation.provider_asset_code != mapping.provider_asset_code
            or observation.observed_amount_atomic != expected_atomic
        ):
            raise ProviderReconciliationConflict(
                "verified observation conflicts with immutable mapping binding"
            )

        existing = await self.session.scalar(
            select(ProviderPaymentObservation)
            .where(
                ProviderPaymentObservation.provider_account_identity
                == mapping.provider_account_identity,
                ProviderPaymentObservation.provider_network == mapping.provider_network,
                ProviderPaymentObservation.provider_invoice_id == observation.provider_invoice_id,
            )
            .with_for_update()
        )
        if existing is not None:
            if (
                existing.merchant_id != actor_merchant_id
                or existing.mapping_id != mapping.id
                or existing.rail_id != mapping.rail_id
                or existing.provider_network != mapping.provider_network
                or existing.ledger_asset_id != mapping.ledger_asset_id
                or _db_atomic(existing.observed_amount_atomic, "stored observed atomic amount")
                != observation.observed_amount_atomic
                or existing.economic_digest != observation.economic_digest
            ):
                raise ProviderReconciliationConflict(
                    "provider identity conflicts with immutable economic observation"
                )
            await self._assert_completed_projection(
                actor_merchant_id, mapping, observation, existing
            )
            return existing

        invoice = await self.session.scalar(
            select(Invoice)
            .where(Invoice.id == mapping.invoice_id, Invoice.merchant_id == actor_merchant_id)
            .with_for_update()
        )
        rail = await self.session.scalar(
            select(Rail)
            .where(Rail.id == mapping.rail_id, Rail.merchant_id == actor_merchant_id)
            .with_for_update()
        )
        asset = await self.session.scalar(
            select(LedgerAsset).where(LedgerAsset.id == mapping.ledger_asset_id)
        )
        if invoice is None or rail is None or asset is None:
            raise ProviderIngestionError("tenant mapping parent mismatch")
        if (
            not invoice.is_payable
            or invoice.asset != mapping.provider_asset_code
            or _atomic(invoice.amount, asset.atomic_decimals) != expected_atomic
        ):
            raise ProviderReconciliationConflict("invoice no longer matches exact mapped payment")
        if (
            not rail.is_active
            or rail.rail_type != "cryptobot"
            or rail.network != mapping.provider_network
            or mapping.provider_asset_code not in rail.assets
            or asset.network_kind != "custodial"
            or asset.network_identifier != f"cryptobot:{mapping.provider_network}"
            or asset.canonical_identifier != mapping.provider_asset_code
            or asset.is_native
        ):
            raise ProviderReconciliationConflict(
                "rail or canonical asset no longer matches mapping"
            )

        fact = ProviderPaymentObservation(
            merchant_id=actor_merchant_id,
            mapping_id=mapping.id,
            rail_id=mapping.rail_id,
            provider_account_identity=mapping.provider_account_identity,
            provider_network=mapping.provider_network,
            provider_invoice_id=observation.provider_invoice_id,
            ledger_asset_id=mapping.ledger_asset_id,
            observed_amount_atomic=observation.observed_amount_atomic,
            observed_at=datetime.now(UTC),
            source_kind=observation.source_kind,
            economic_digest=observation.economic_digest,
            evidence_digest=observation.evidence_digest,
        )
        self.session.add(fact)
        await self.session.flush()
        decision_digest = _digest({"observation_id": str(fact.id), "decision": "verified"})
        decision = ProviderVerificationDecision(
            merchant_id=actor_merchant_id,
            observation_id=fact.id,
            decision="verified",
            decision_digest=decision_digest,
        )
        self.session.add(decision)
        transaction = await LedgerPostingService(self.session).post_in_transaction(
            merchant_id=actor_merchant_id,
            source_namespace="provider_payment_observation",
            source_external_id=observation.economic_digest,
            source_digest=observation.economic_digest,
            idempotency_key=f"provider-observation:{observation.economic_digest}",
            lines=(
                LedgerLine(
                    mapping.receivable_account_id,
                    mapping.ledger_asset_id,
                    LedgerDirection.DEBIT,
                    observation.observed_amount_atomic,
                ),
                LedgerLine(
                    mapping.payable_account_id,
                    mapping.ledger_asset_id,
                    LedgerDirection.CREDIT,
                    observation.observed_amount_atomic,
                ),
            ),
        )
        invoice.status = InvoiceStatus.CONFIRMED
        event_id = observation.economic_digest
        payload = {
            "event": "invoice.confirmed",
            "event_id": event_id,
            "invoice": {
                "id": str(invoice.id),
                "public_id": invoice.public_id,
                "status": invoice.status.value,
            },
            "observed": {
                "ledger_asset_id": str(mapping.ledger_asset_id),
                "network_kind": asset.network_kind,
                "network_identifier": asset.network_identifier,
                "atomic_decimals": asset.atomic_decimals,
                "provider_account_identity": mapping.provider_account_identity,
                "provider_network": mapping.provider_network,
                "asset_code": mapping.provider_asset_code,
                "amount_atomic": str(observation.observed_amount_atomic),
            },
        }
        invoice_event = InvoiceEvent(
            invoice_id=invoice.id,
            event_id=event_id,
            event_type="invoice.confirmed",
            payload=payload,
        )
        self.session.add(invoice_event)
        webhooks = (
            await self.session.scalars(
                select(Webhook).where(
                    Webhook.merchant_id == actor_merchant_id,
                    Webhook.is_active.is_(True),
                )
            )
        ).all()
        outboxes: list[OutboxWebhook] = []
        for webhook in webhooks:
            if "invoice.confirmed" in webhook.events:
                outbox = OutboxWebhook(
                    webhook_id=webhook.id,
                    invoice_id=invoice.id,
                    event_type="invoice.confirmed",
                    payload=payload,
                    status=OutboxStatus.PENDING,
                )
                self.session.add(outbox)
                outboxes.append(outbox)
        await self.session.flush()
        receipt = ProviderPaymentReceipt(
            merchant_id=actor_merchant_id,
            observation_id=fact.id,
            decision_id=decision.id,
            ledger_transaction_id=transaction.id,
            invoice_id=invoice.id,
            invoice_event_id=invoice_event.id,
            event_id=event_id,
            intended_recipient_count=len(outboxes),
        )
        self.session.add(receipt)
        await self.session.flush()
        for outbox in outboxes:
            webhook = next(webhook for webhook in webhooks if webhook.id == outbox.webhook_id)
            self.session.add(
                ProviderPaymentRecipientIntent(
                    merchant_id=actor_merchant_id,
                    receipt_id=receipt.id,
                    outbox_id=outbox.id,
                    webhook_id=outbox.webhook_id,
                    destination_url=webhook.url,
                    payload_snapshot=payload,
                    destination_digest=hashlib.sha256(webhook.url.encode()).hexdigest(),
                    payload_digest=await _outbox_snapshot_digest(self.session, outbox.id),
                )
            )
        await self.session.flush()
        return fact
