"""
Сервис для работы с платежами и депозитными адресами.
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_chain_config, get_token_contract
from src.core.config import get_settings
from src.core.exceptions import (
    InvoiceExpiredError,
    PaymentError,
    ValidationError,
)
from src.crypto.encryption import encrypt_private_key
from src.crypto.hd_wallet import HDWallet
from src.db.models import (
    DepositAddress,
    Invoice,
    InvoiceStatus,
    OnchainTx,
    PaymentSession,
    SweepJob,
    SweepState,
    TxStatus,
)
from src.services.invoice_service import InvoiceService

logger = logging.getLogger(__name__)


class PaymentService:
    """Сервис для работы с платежами."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()
        self._hd_wallet: HDWallet | None = None

    @property
    def hd_wallet(self) -> HDWallet:
        """Lazy-загрузка HD кошелька."""
        if self._hd_wallet is None:
            self._hd_wallet = HDWallet(self.settings.hd_wallet_seed.get_secret_value())
        return self._hd_wallet

    async def select_payment_option(
        self,
        invoice: Invoice,
        chain: str,
        token: str,
    ) -> PaymentSession:
        """
        Выбрать сеть и токен для оплаты.
        Создаёт payment session с назначенным deposit address.

        Args:
            invoice: Инвойс
            chain: Выбранная сеть
            token: Выбранный токен

        Returns:
            Созданная payment session

        Raises:
            InvoiceExpiredError: Если инвойс истёк
            ValidationError: Если сеть/токен не разрешены
        """
        # Проверяем статус инвойса
        if invoice.status not in (
            InvoiceStatus.CREATED,
            InvoiceStatus.AWAITING_PAYMENT,
        ):
            raise PaymentError(
                f"Invoice is in status {invoice.status}, cannot select payment option"
            )

        # Проверяем, не истёк ли инвойс
        # Учитываем, что SQLite может вернуть naive datetime
        expires_at = invoice.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            raise InvoiceExpiredError(invoice.public_id)

        # Проверяем, что сеть разрешена
        chain = chain.lower()
        if chain not in invoice.allowed_chains:
            raise ValidationError(
                f"Chain {chain} is not allowed for this invoice",
                details={"allowed_chains": invoice.allowed_chains},
            )

        # Проверяем, что токен совпадает с asset инвойса
        token = token.upper()
        if token != invoice.asset:
            raise ValidationError(
                f"Token {token} does not match invoice asset {invoice.asset}",
            )

        # Проверяем, нет ли уже сессии для этой комбинации
        existing = await self._get_existing_session(invoice.id, chain, token)
        if existing:
            return existing

        # Получаем или создаём deposit address
        deposit_address = await self._get_or_create_deposit_address()

        # Создаём payment session
        session = PaymentSession(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            chain=chain,
            token=token,
            deposit_address_id=deposit_address.id,
        )
        self.session.add(session)

        # Помечаем адрес как используемый
        deposit_address.is_used = True

        # Обновляем статус инвойса
        if invoice.status == InvoiceStatus.CREATED:
            invoice.status = InvoiceStatus.AWAITING_PAYMENT

        await self.session.commit()
        await self.session.refresh(session)

        # Загружаем связанный deposit_address
        await self.session.refresh(session, ["deposit_address"])

        logger.info(
            f"Created payment session for invoice {invoice.id}: "
            f"chain={chain}, token={token}, address={deposit_address.address}"
        )

        return session

    async def record_onchain_tx(
        self,
        payment_session: PaymentSession,
        tx_hash: str,
        block_number: int,
        log_index: int,
        from_address: str,
        amount: Decimal,
    ) -> OnchainTx:
        """
        Записать найденную onchain транзакцию.

        Args:
            payment_session: Сессия оплаты
            tx_hash: Хеш транзакции
            block_number: Номер блока
            log_index: Индекс лога
            from_address: Адрес отправителя
            amount: Сумма

        Returns:
            Созданная запись OnchainTx
        """
        # Проверяем, не записана ли уже эта транзакция
        existing = await self._get_onchain_tx(payment_session.chain, tx_hash, log_index)
        if existing:
            return existing

        # Получаем адрес токена
        token_contract = get_token_contract(
            payment_session.chain, payment_session.token
        )

        # Создаём запись
        onchain_tx = OnchainTx(
            id=uuid.uuid4(),
            chain=payment_session.chain,
            tx_hash=tx_hash,
            block_number=block_number,
            log_index=log_index,
            from_address=from_address.lower(),
            to_address=payment_session.deposit_address.address.lower(),
            token_contract=token_contract.lower(),
            amount=amount,
            payment_session_id=payment_session.id,
            status=TxStatus.PENDING,
            confirmations=0,
        )
        self.session.add(onchain_tx)
        await self.session.commit()

        logger.info(
            f"Recorded onchain tx {tx_hash} for session {payment_session.id}: "
            f"amount={amount}, block={block_number}"
        )

        return onchain_tx

    async def update_tx_confirmations(
        self,
        onchain_tx: OnchainTx,
        confirmations: int,
        required_confirmations: int,
    ) -> bool:
        """
        Обновить количество подтверждений транзакции.

        Returns:
            True если транзакция теперь подтверждена
        """
        onchain_tx.confirmations = confirmations

        if onchain_tx.status == TxStatus.PENDING:
            onchain_tx.status = TxStatus.CONFIRMING

        # Проверяем, достаточно ли подтверждений
        if (
            confirmations >= required_confirmations
            and onchain_tx.status != TxStatus.CONFIRMED
        ):
            onchain_tx.status = TxStatus.CONFIRMED
            onchain_tx.confirmed_at = datetime.now(timezone.utc)
            await self.session.commit()
            return True

        await self.session.commit()
        return False

    async def process_confirmed_payment(
        self,
        payment_session: PaymentSession,
        invoice: Invoice,
    ) -> None:
        """
        Обработать подтверждённый платёж.
        Обновляет статус инвойса и создаёт sweep job.
        """
        # Обновляем статус инвойса
        invoice_service = InvoiceService(self.session)
        await invoice_service.update_invoice_status(
            invoice,
            InvoiceStatus.CONFIRMED,
            event_payload={
                "chain": payment_session.chain,
                "token": payment_session.token,
            },
        )

        # Создаём sweep job
        sweep_job = SweepJob(
            id=uuid.uuid4(),
            payment_session_id=payment_session.id,
            state=SweepState.PENDING_GAS,
        )
        self.session.add(sweep_job)
        await self.session.commit()

        logger.info(f"Invoice {invoice.id} confirmed, created sweep job {sweep_job.id}")

    async def _get_existing_session(
        self,
        invoice_id: uuid.UUID,
        chain: str,
        token: str,
    ) -> PaymentSession | None:
        """Получить существующую payment session."""
        stmt = (
            select(PaymentSession)
            .options(selectinload(PaymentSession.deposit_address))
            .where(
                and_(
                    PaymentSession.invoice_id == invoice_id,
                    PaymentSession.chain == chain,
                    PaymentSession.token == token,
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_or_create_deposit_address(self) -> DepositAddress:
        """
        Получить свободный или создать новый deposit address.
        Использует SELECT FOR UPDATE для предотвращения race condition.
        """
        # Сначала пробуем найти неиспользуемый адрес с блокировкой
        stmt = (
            select(DepositAddress)
            .where(
                and_(
                    DepositAddress.chain_group == "evm",
                    DepositAddress.is_used == False,  # noqa: E712
                )
            )
            .with_for_update(skip_locked=True)  # Пропустить заблокированные записи
            .limit(1)
        )
        result = await self.session.execute(stmt)
        address = result.scalar_one_or_none()

        if address:
            logger.info(f"Reusing existing deposit address {address.address}")
            return address

        # Создаём новый адрес с блокировкой на max index
        return await self._create_new_deposit_address()

    async def _create_new_deposit_address(self) -> DepositAddress:
        """
        Создать новый deposit address с защитой от race condition.

        Использует Redis INCR для атомарного получения следующего индекса.
        Это гарантирует уникальность индекса даже при параллельных запросах.
        """
        from src.db.redis import get_redis
        from src.db.session import get_session_factory

        # Получаем Redis клиент из пула (без создания нового соединения)
        redis_client = await get_redis()

        session_factory = get_session_factory()

        # Атомарно получаем следующий индекс через Redis INCR
        # Если ключа нет, он создаётся со значением 0 и сразу инкрементируется до 1
        next_index = await redis_client.incr("deposit_address:next_index")
        # Переводим в 0-based (INCR начинает с 1)
        next_index = next_index - 1

        logger.info(f"Creating deposit address at index {next_index} (via Redis)")

        # Деривируем ключ
        derived = self.hd_wallet.derive_key(next_index)

        # Шифруем приватный ключ
        encrypted_privkey = encrypt_private_key(
            derived.private_key,
            self.settings.encryption_key.get_secret_value(),
        )

        # Создаём запись в изолированной сессии с немедленным commit
        async with session_factory() as isolated_session:
            address_id = uuid.uuid4()
            address = DepositAddress(
                id=address_id,
                address=derived.address.lower(),
                encrypted_privkey=encrypted_privkey,
                chain_group="evm",
                derivation_path=derived.derivation_path,
                derivation_index=next_index,
                is_used=False,
            )
            isolated_session.add(address)
            await isolated_session.commit()

        logger.info(f"Created deposit address {derived.address} at index {next_index}")

        # Загружаем созданный адрес в основную сессию
        stmt = select(DepositAddress).where(DepositAddress.id == address_id)
        result = await self.session.execute(stmt)
        address_in_main = result.scalar_one()

        return address_in_main

    async def _get_onchain_tx(
        self,
        chain: str,
        tx_hash: str,
        log_index: int,
    ) -> OnchainTx | None:
        """Получить onchain tx по хешу."""
        stmt = select(OnchainTx).where(
            and_(
                OnchainTx.chain == chain,
                OnchainTx.tx_hash == tx_hash,
                OnchainTx.log_index == log_index,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_payment_sessions(self, chain: str) -> list[PaymentSession]:
        """
        Получить все активные payment sessions для сети.
        Активные = инвойс не истёк и не подтверждён.
        """
        stmt = (
            select(PaymentSession)
            .join(Invoice)
            .options(
                selectinload(PaymentSession.deposit_address),
                selectinload(PaymentSession.invoice),
            )
            .where(
                and_(
                    PaymentSession.chain == chain,
                    Invoice.status.in_(
                        [InvoiceStatus.AWAITING_PAYMENT, InvoiceStatus.SEEN_ONCHAIN]
                    ),
                    Invoice.expires_at > datetime.now(timezone.utc),
                )
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
