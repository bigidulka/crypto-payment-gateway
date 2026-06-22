"""
Сервис для работы с платежами и депозитными адресами.
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.blockchain.chains import ChainType, get_chain_config, get_token_contract
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
    PaymentSessionStatus,
    SweepSource,
    SweepState,
    TxStatus,
    UnifiedSweepJob,
)
from src.services.address_lease_service import AddressLeaseService
from src.services.invoice_service import InvoiceService

logger = logging.getLogger(__name__)


class PaymentService:
    """Сервис для работы с платежами."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()
        self.address_leases = AddressLeaseService(session)
        self._hd_wallets: dict[str, Any] = {}  # chain_group -> wallet

    def _get_hd_wallet(self, chain_group: str = "evm") -> Any:
        """
        Lazy-загрузка HD кошелька для chain_group.

        Args:
            chain_group: 'evm' | 'solana' | 'ton'

        Returns:
            HD wallet для данной группы сетей
        """
        if chain_group not in self._hd_wallets:
            if chain_group == "evm":
                self._hd_wallets[chain_group] = HDWallet(
                    self.settings.hd_wallet_seed.get_secret_value()
                )
            elif chain_group == "solana":
                from src.crypto.solana_wallet import SolanaHDWallet
                seed = self.settings.solana_wallet_seed.get_secret_value()
                if not seed:
                    seed = self.settings.hd_wallet_seed.get_secret_value()
                self._hd_wallets[chain_group] = SolanaHDWallet(seed)
            elif chain_group == "ton":
                from src.crypto.ton_wallet import TonHDWallet
                seed = self.settings.ton_wallet_seed.get_secret_value()
                if not seed:
                    seed = self.settings.hd_wallet_seed.get_secret_value()
                self._hd_wallets[chain_group] = TonHDWallet(seed)
            else:
                raise ValueError(f"Unknown chain_group: {chain_group}")
        return self._hd_wallets[chain_group]

    @property
    def hd_wallet(self) -> HDWallet:
        """Backward compatibility: EVM HD кошелёк."""
        return self._get_hd_wallet("evm")

    def _get_chain_group(self, chain: str) -> str:
        """Определить chain_group по имени сети."""
        config = get_chain_config(chain)
        if config.chain_type == ChainType.EVM:
            return "evm"
        elif config.chain_type == ChainType.SOLANA:
            return "solana"
        elif config.chain_type == ChainType.TON:
            return "ton"
        else:
            return "evm"  # fallback

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

        # Определяем chain_group для выбранной сети
        chain_group = self._get_chain_group(chain)

        # Получаем lease на deposit address для этой группы
        session_id = uuid.uuid4()
        deposit_address = await self._acquire_deposit_address_lease(
            chain_group,
            invoice.expires_at,
        )

        # Создаём payment session
        session = PaymentSession(
            id=session_id,
            invoice_id=invoice.id,
            chain=chain,
            token=token,
            deposit_address_id=deposit_address.id,
            status=PaymentSessionStatus.PENDING,
            expires_at=invoice.expires_at,
        )
        self.session.add(session)
        await self.session.flush()
        await self.address_leases.bind_payment_session(
            deposit_address,
            session.id,
            reason="payment_session_created",
        )

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
        # Normalize tx_hash to always have 0x prefix
        if tx_hash and not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash

        # Проверяем, не записана ли уже эта транзакция
        existing = await self._get_onchain_tx(payment_session.chain, tx_hash, log_index)
        if existing:
            return existing

        # Получаем адрес токена
        token_contract = get_token_contract(
            payment_session.chain, payment_session.token
        )

        # Определяем chain_group для нормализации адресов
        chain_group = self._get_chain_group(payment_session.chain)

        # Нормализация адресов (EVM = lowercase, non-EVM = as-is)
        if chain_group == "evm":
            normalized_from = from_address.lower()
            normalized_to = payment_session.deposit_address.address.lower()
            normalized_token = token_contract.lower()
        else:
            normalized_from = from_address
            normalized_to = payment_session.deposit_address.address
            normalized_token = token_contract

        # Создаём запись
        onchain_tx = OnchainTx(
            id=uuid.uuid4(),
            chain=payment_session.chain,
            tx_hash=tx_hash,
            block_number=block_number,
            log_index=log_index,
            from_address=normalized_from,
            to_address=normalized_to,
            token_contract=normalized_token,
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
        Обновляет статус инвойса и создаёт UnifiedSweepJob.
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

        sweep_job = await self._create_invoice_sweep_job(
            payment_session,
            invoice,
            priority=50 if invoice.amount >= 100 else 10,
        )
        await self.address_leases.release_to_cooldown(
            payment_session,
            self.address_leases.cooldown_until_for_invoice(invoice),
            status=PaymentSessionStatus.PAID,
            reason="invoice_confirmed",
        )
        await self.session.commit()

        logger.info(f"Invoice {invoice.id} confirmed, created sweep job {sweep_job.id}")

    async def process_late_payment(
        self,
        payment_session: PaymentSession,
        invoice: Invoice,
    ) -> None:
        """
        Обработать подтверждённый late payment.
        Инвойс не подтверждается, средства ставятся в sweep для ручной сверки.
        """
        if payment_session.status != PaymentSessionStatus.LATE:
            await self.address_leases.mark_late_deposit(
                payment_session,
                reason="late_payment_confirmed",
            )
        if payment_session.paid_at is None:
            payment_session.paid_at = datetime.now(timezone.utc)

        sweep_job = await self._create_invoice_sweep_job(
            payment_session,
            invoice,
            priority=0,
        )
        await self.session.commit()

        logger.warning(
            f"Late payment for expired invoice {invoice.id}; "
            f"session={payment_session.id}, sweep_job={sweep_job.id}"
        )

    async def _create_invoice_sweep_job(
        self,
        payment_session: PaymentSession,
        invoice: Invoice,
        *,
        priority: int,
    ) -> UnifiedSweepJob:
        existing = await self._get_invoice_sweep_job(payment_session.id)
        if existing is not None:
            return existing

        config = get_chain_config(payment_session.chain)
        token_config = config.tokens.get(payment_session.token)
        decimals = token_config.decimals if token_config else 6
        deposit_addr = payment_session.deposit_address

        sweep_job = UnifiedSweepJob(
            id=uuid.uuid4(),
            source=SweepSource.INVOICE,
            source_id=payment_session.id,
            chain=payment_session.chain,
            token=payment_session.token,
            token_contract=token_config.contract_address if token_config else "",
            from_address=deposit_addr.address,
            to_address=self.settings.get_treasury_address(payment_session.chain),
            encrypted_private_key=(
                deposit_addr.encrypted_privkey.hex()
                if isinstance(deposit_addr.encrypted_privkey, bytes)
                else deposit_addr.encrypted_privkey
            ),
            amount=invoice.amount,
            amount_raw=str(int(invoice.amount * (10**decimals))),
            state=SweepState.PENDING_GAS,
            priority=priority,
        )
        self.session.add(sweep_job)
        return sweep_job

    async def _get_invoice_sweep_job(
        self,
        payment_session_id: uuid.UUID,
    ) -> UnifiedSweepJob | None:
        stmt = select(UnifiedSweepJob).where(
            and_(
                UnifiedSweepJob.source == SweepSource.INVOICE,
                UnifiedSweepJob.source_id == payment_session_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

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

    async def _acquire_deposit_address_lease(
        self,
        chain_group: str,
        leased_until: datetime,
    ) -> DepositAddress:
        """
        Получить lease на свободный или новый deposit address.
        Использует SELECT FOR UPDATE SKIP LOCKED внутри AddressLeaseService.
        """
        address = await self.address_leases.acquire_available_address(
            chain_group,
            leased_until,
        )
        if address:
            logger.info(f"Leased existing deposit address {address.address}")
            return address

        for _ in range(2):
            created = await self._create_new_deposit_address(chain_group)
            leased = await self.address_leases.acquire_address_by_id(
                created.id,
                leased_until,
            )
            if leased is not None:
                logger.info(f"Leased new deposit address {leased.address}")
                return leased

        raise PaymentError("Unable to acquire deposit address lease")

    async def _create_new_deposit_address(
        self, chain_group: str = "evm"
    ) -> DepositAddress:
        """
        Создать новый deposit address с защитой от race condition.

        Использует Redis INCR для атомарного получения следующего индекса.
        Это гарантирует уникальность индекса даже при параллельных запросах.

        Args:
            chain_group: 'evm' | 'solana' | 'ton'
        """
        from src.db.redis import get_redis
        from src.db.session import get_session_factory

        # Получаем Redis клиент из пула (без создания нового соединения)
        redis_client = await get_redis()

        session_factory = get_session_factory()

        # Атомарно получаем следующий индекс через Redis INCR
        # Используем отдельный ключ для каждой группы сетей
        redis_key = f"deposit_address:{chain_group}:next_index"
        next_index = await redis_client.incr(redis_key)
        # Переводим в 0-based (INCR начинает с 1)
        next_index = next_index - 1

        logger.info(
            f"Creating {chain_group} deposit address at index {next_index} (via Redis)"
        )

        # Деривируем ключ с помощью соответствующего HD wallet
        hd_wallet = self._get_hd_wallet(chain_group)
        derived = hd_wallet.derive_key(next_index)

        # Получаем адрес (формат зависит от chain_group)
        if chain_group == "evm":
            address = derived.address.lower()
        else:
            # Solana/TON — адреса case-sensitive
            address = derived.address

        # Шифруем приватный ключ
        encrypted_privkey = encrypt_private_key(
            derived.private_key,
            self.settings.encryption_key.get_secret_value(),
        )

        # Создаём запись в изолированной сессии с немедленным commit
        async with session_factory() as isolated_session:
            address_id = uuid.uuid4()
            deposit_address = DepositAddress(
                id=address_id,
                address=address,
                encrypted_privkey=encrypted_privkey,
                chain_group=chain_group,
                derivation_path=derived.derivation_path,
                derivation_index=next_index,
                is_used=False,
            )
            isolated_session.add(deposit_address)
            await isolated_session.commit()

        logger.info(
            f"Created {chain_group} deposit address {address} at index {next_index}"
        )

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
        return await self.address_leases.get_active_sessions_for_chain(chain)
