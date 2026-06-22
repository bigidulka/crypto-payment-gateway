"""
Сервис lease lifecycle для reusable deposit address pool.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import PaymentError
from src.db.models import AddressLeaseEvent, DepositAddress, PaymentSession
from src.db.models.enums import DepositAddressLeaseStatus, PaymentSessionStatus


class AddressLeaseService:
    """Управляет atomic lease/release/cooldown lifecycle deposit addresses."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def acquire_available_address(
        self,
        chain_group: str,
        leased_until: datetime,
        *,
        now: datetime | None = None,
    ) -> DepositAddress | None:
        """
        Атомарно взять свободный адрес из pool.

        Commit не выполняется: caller создаёт PaymentSession и коммитит одну транзакцию.
        """
        now = self._utc_now(now)
        leased_until = self._ensure_aware(leased_until)
        await self.promote_ready_cooldowns(now=now)

        stmt = (
            select(DepositAddress)
            .where(
                and_(
                    DepositAddress.chain_group == chain_group,
                    DepositAddress.lease_status == DepositAddressLeaseStatus.AVAILABLE,
                    DepositAddress.is_used == False,  # noqa: E712
                    DepositAddress.retired_at.is_(None),
                    or_(
                        DepositAddress.cooldown_until.is_(None),
                        DepositAddress.cooldown_until <= now,
                    ),
                )
            )
            .order_by(DepositAddress.last_used_at.asc(), DepositAddress.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        address = result.scalar_one_or_none()
        if address is None:
            return None

        await self._mark_leased(address, leased_until, now)
        return address

    async def acquire_address_by_id(
        self,
        address_id: uuid.UUID,
        leased_until: datetime,
        *,
        now: datetime | None = None,
    ) -> DepositAddress | None:
        """Взять конкретный адрес, если он всё ещё free после генерации."""
        now = self._utc_now(now)
        leased_until = self._ensure_aware(leased_until)

        stmt = (
            select(DepositAddress)
            .where(DepositAddress.id == address_id)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        address = result.scalar_one_or_none()
        if address is None or not self._is_available(address, now):
            return None

        await self._mark_leased(address, leased_until, now)
        return address

    async def bind_payment_session(
        self,
        address: DepositAddress,
        payment_session_id: uuid.UUID,
        *,
        reason: str,
    ) -> None:
        """Записать audit event после flush PaymentSession."""
        self.session.add(
            AddressLeaseEvent(
                id=uuid.uuid4(),
                deposit_address_id=address.id,
                payment_session_id=payment_session_id,
                event_type="lease_bound",
                previous_status=DepositAddressLeaseStatus.LEASED.value,
                new_status=DepositAddressLeaseStatus.LEASED.value,
                reason=reason,
                payload={"leased_until": self._iso_or_none(address.leased_until)},
            )
        )

    async def release_to_cooldown(
        self,
        payment_session: PaymentSession,
        cooldown_until: datetime,
        *,
        status: PaymentSessionStatus,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Освободить session address в cooldown, не делая address available сразу."""
        now = self._utc_now(now)
        cooldown_until = self._ensure_aware(cooldown_until)
        payment_session.status = status
        payment_session.released_at = now
        if status == PaymentSessionStatus.PAID and payment_session.paid_at is None:
            payment_session.paid_at = now

        address = await self._locked_address(payment_session.deposit_address_id)
        previous = self._status_value(address.lease_status)
        address.lease_status = DepositAddressLeaseStatus.COOLDOWN
        address.is_used = True
        address.leased_until = None
        address.cooldown_until = cooldown_until
        address.last_used_at = now

        await self._add_event(
            address,
            payment_session.id,
            event_type="lease_released_to_cooldown",
            previous_status=previous,
            new_status=DepositAddressLeaseStatus.COOLDOWN.value,
            reason=reason,
            payload={
                "payment_session_status": status.value,
                "cooldown_until": cooldown_until.isoformat(),
            },
        )

    async def mark_late_deposit(
        self,
        payment_session: PaymentSession,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Отметить payment session как late после поступления вне active window."""
        now = self._utc_now(now)
        previous_status = self._status_value(payment_session.status)
        payment_session.status = PaymentSessionStatus.LATE
        payment_session.released_at = payment_session.released_at or now

        address = await self._locked_address(payment_session.deposit_address_id)
        await self._add_event(
            address,
            payment_session.id,
            event_type="late_deposit_detected",
            previous_status=self._status_value(address.lease_status),
            new_status=self._status_value(address.lease_status),
            reason=reason,
            payload={"previous_payment_session_status": previous_status},
        )

    async def promote_ready_cooldowns(
        self,
        *,
        now: datetime | None = None,
        limit: int = 500,
    ) -> int:
        """Перевести cooldown addresses в available после cooldown_until."""
        now = self._utc_now(now)
        stmt = (
            select(DepositAddress)
            .where(
                and_(
                    DepositAddress.lease_status == DepositAddressLeaseStatus.COOLDOWN,
                    DepositAddress.cooldown_until.is_not(None),
                    DepositAddress.cooldown_until <= now,
                    DepositAddress.retired_at.is_(None),
                )
            )
            .order_by(DepositAddress.cooldown_until.asc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        addresses = list(result.scalars().all())

        for address in addresses:
            previous = self._status_value(address.lease_status)
            address.lease_status = DepositAddressLeaseStatus.AVAILABLE
            address.is_used = False
            address.leased_until = None
            address.cooldown_until = None
            await self._add_event(
                address,
                None,
                event_type="cooldown_completed",
                previous_status=previous,
                new_status=DepositAddressLeaseStatus.AVAILABLE.value,
                reason="cooldown_elapsed",
                payload=None,
            )

        return len(addresses)

    def cooldown_until_for_invoice(self, invoice) -> datetime:
        """Рассчитать cooldown horizon из invoice expiry + invoice TTL."""
        expires_at = self._ensure_aware(invoice.expires_at)
        return expires_at + timedelta(minutes=invoice.ttl_minutes)

    async def get_active_sessions_for_chain(self, chain: str) -> list[PaymentSession]:
        """Получить active payment checks для scanner."""
        stmt = (
            select(PaymentSession)
            .options(
                selectinload(PaymentSession.deposit_address),
                selectinload(PaymentSession.invoice),
            )
            .where(
                and_(
                    PaymentSession.chain == chain,
                    PaymentSession.status.in_(
                        [
                            PaymentSessionStatus.PENDING,
                            PaymentSessionStatus.SEEN_ONCHAIN,
                        ]
                    ),
                    PaymentSession.expires_at > datetime.now(UTC),
                )
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def _mark_leased(
        self,
        address: DepositAddress,
        leased_until: datetime,
        now: datetime,
    ) -> None:
        previous = self._status_value(address.lease_status)
        address.lease_status = DepositAddressLeaseStatus.LEASED
        address.is_used = True
        address.leased_until = leased_until
        address.cooldown_until = None
        address.last_used_at = now
        await self._add_event(
            address,
            None,
            event_type="lease_acquired",
            previous_status=previous,
            new_status=DepositAddressLeaseStatus.LEASED.value,
            reason="payment_session_address_acquired",
            payload={"leased_until": leased_until.isoformat()},
        )

    async def _locked_address(self, address_id: uuid.UUID) -> DepositAddress:
        stmt = (
            select(DepositAddress)
            .where(DepositAddress.id == address_id)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        address = result.scalar_one_or_none()
        if address is None:
            raise PaymentError(f"Deposit address {address_id} is not available for lease")
        return address

    async def _add_event(
        self,
        address: DepositAddress,
        payment_session_id: uuid.UUID | None,
        *,
        event_type: str,
        previous_status: str | None,
        new_status: str | None,
        reason: str,
        payload: dict[str, Any] | None,
    ) -> None:
        self.session.add(
            AddressLeaseEvent(
                id=uuid.uuid4(),
                deposit_address_id=address.id,
                payment_session_id=payment_session_id,
                event_type=event_type,
                previous_status=previous_status,
                new_status=new_status,
                reason=reason,
                payload=payload,
            )
        )

    def _is_available(self, address: DepositAddress, now: datetime) -> bool:
        cooldown_until = address.cooldown_until
        if cooldown_until is not None:
            cooldown_until = self._ensure_aware(cooldown_until)
        return (
            address.lease_status == DepositAddressLeaseStatus.AVAILABLE
            and not address.is_used
            and address.retired_at is None
            and (cooldown_until is None or cooldown_until <= now)
        )

    @staticmethod
    def _utc_now(now: datetime | None) -> datetime:
        return now if now is not None else datetime.now(UTC)

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _status_value(value: Any) -> str | None:
        if value is None:
            return None
        return value.value if hasattr(value, "value") else str(value)

    @staticmethod
    def _iso_or_none(value: datetime | None) -> str | None:
        return value.isoformat() if value else None
