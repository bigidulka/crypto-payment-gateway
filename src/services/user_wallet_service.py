"""
Сервис для управления User Wallets и Deposits.

Обеспечивает:
- Создание постоянных кошельков для пользователей
- Генерация адресов во всех сетях
- Обработка депозитов
- Управление балансами
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.blockchain.chains import get_all_chains, get_chain_config
from src.core.config import get_settings
from src.core.deposit_security import validate_deposit, ValidationResult
from src.crypto.hd_wallet import HDWallet
from src.db.models import (
    Deposit,
    DepositStatus,
    Merchant,
    UserBalance,
    UserWallet,
    WalletAddress,
)
from src.db.redis import get_redis

logger = logging.getLogger(__name__)


class UserWalletService:
    """Сервис для работы с кошельками пользователей."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._hd_wallet: HDWallet | None = None

    async def _get_hd_wallet(self) -> HDWallet:
        """Получить HD Wallet (lazy init)."""
        if self._hd_wallet is None:
            settings = get_settings()
            mnemonic = settings.hd_wallet_seed.get_secret_value()
            if not mnemonic:
                raise ValueError("HD_WALLET_SEED is not configured")
            self._hd_wallet = HDWallet(mnemonic)
        return self._hd_wallet

    async def _get_next_derivation_index(self) -> int:
        """
        Получить следующий derivation index атомарно через Redis.
        Использует INCR для атомарности.
        """
        redis = await get_redis()
        # Используем отдельный счётчик для persistent wallets
        next_index = await redis.incr("user_wallet:next_derivation_index")
        # INCR возвращает значение ПОСЛЕ инкремента, поэтому вычитаем 1
        return next_index - 1

    async def get_or_create_wallet(
        self,
        merchant_id: uuid.UUID,
        external_user_id: str,
        user_metadata: dict | None = None,
    ) -> UserWallet:
        """
        Получить или создать кошелёк для пользователя.

        При создании автоматически генерирует адреса во всех сетях.

        Args:
            merchant_id: ID мерчанта
            external_user_id: Внешний ID пользователя (telegram_id, user_id и т.д.)
            user_metadata: Дополнительные метаданные

        Returns:
            UserWallet с адресами во всех сетях
        """
        # Пробуем найти существующий кошелёк
        stmt = (
            select(UserWallet)
            .options(
                selectinload(UserWallet.addresses),
                selectinload(UserWallet.balances),
            )
            .where(
                and_(
                    UserWallet.merchant_id == merchant_id,
                    UserWallet.external_user_id == external_user_id,
                )
            )
        )
        result = await self.session.execute(stmt)
        wallet = result.scalar_one_or_none()

        if wallet:
            logger.debug(f"Found existing wallet for user {external_user_id}")
            return wallet

        # Создаём новый кошелёк
        logger.info(f"Creating new wallet for user {external_user_id}")

        wallet = UserWallet(
            id=uuid.uuid4(),
            merchant_id=merchant_id,
            external_user_id=external_user_id,
            user_metadata=user_metadata,
            is_active=True,
        )
        self.session.add(wallet)

        # Генерируем адреса для всех сетей
        hd_wallet = await self._get_hd_wallet()
        chains = get_all_chains()
        settings = get_settings()
        encryption_key = settings.encryption_key.get_secret_value()

        # Импортируем шифрование
        from src.crypto.encryption import encrypt_private_key

        for chain in chains:
            # Получаем уникальный derivation index
            derivation_index = await self._get_next_derivation_index()

            # Генерируем адрес
            derived = hd_wallet.derive_key(derivation_index)

            # Шифруем приватный ключ
            encrypted_pk = encrypt_private_key(derived.private_key, encryption_key)

            wallet_address = WalletAddress(
                id=uuid.uuid4(),
                user_wallet_id=wallet.id,
                chain=chain,
                address=derived.address,
                derivation_index=derivation_index,
                encrypted_private_key=encrypted_pk.hex(),  # Храним как hex
                is_active=True,
            )
            self.session.add(wallet_address)
            wallet.addresses.append(wallet_address)

            logger.info(
                f"Created address for {chain}: {derived.address[:10]}...{derived.address[-6:]} "
                f"(index={derivation_index})"
            )

        # Создаём балансы для основных активов
        for asset in ["USDT", "USDC"]:
            balance = UserBalance(
                id=uuid.uuid4(),
                user_wallet_id=wallet.id,
                asset=asset,
                balance=Decimal("0"),
                total_deposited=Decimal("0"),
                total_withdrawn=Decimal("0"),
            )
            self.session.add(balance)
            wallet.balances.append(balance)

        await self.session.commit()
        await self.session.refresh(wallet)

        logger.info(
            f"Wallet created for user {external_user_id} with {len(wallet.addresses)} addresses"
        )

        return wallet

    async def get_wallet_by_address(
        self,
        chain: str,
        address: str,
    ) -> UserWallet | None:
        """
        Найти кошелёк по адресу в сети.

        Args:
            chain: Название сети
            address: Deposit адрес

        Returns:
            UserWallet или None
        """
        stmt = (
            select(UserWallet)
            .join(WalletAddress)
            .options(
                selectinload(UserWallet.addresses),
                selectinload(UserWallet.balances),
                selectinload(UserWallet.merchant),
            )
            .where(
                and_(
                    WalletAddress.chain == chain,
                    WalletAddress.address == address.lower(),
                    WalletAddress.is_active == True,
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_wallet(
        self,
        merchant_id: uuid.UUID,
        external_user_id: str,
    ) -> UserWallet | None:
        """Получить кошелёк пользователя."""
        stmt = (
            select(UserWallet)
            .options(
                selectinload(UserWallet.addresses),
                selectinload(UserWallet.balances),
            )
            .where(
                and_(
                    UserWallet.merchant_id == merchant_id,
                    UserWallet.external_user_id == external_user_id,
                )
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def record_deposit(
        self,
        wallet_address: WalletAddress,
        tx_hash: str,
        block_number: int,
        log_index: int,
        amount: Decimal,
        asset: str,
        token_contract: str,
        from_address: str,
        required_confirmations: int,
    ) -> Deposit | None:
        """
        Записать обнаруженный депозит.

        Args:
            wallet_address: Адрес кошелька
            tx_hash: Хеш транзакции
            block_number: Номер блока
            log_index: Индекс лога
            amount: Сумма
            asset: Актив (USDT/USDC)
            token_contract: Адрес контракта токена
            from_address: Адрес отправителя
            required_confirmations: Требуемое кол-во подтверждений

        Returns:
            Созданный Deposit или None если отклонён по безопасности
        """
        # === ВАЛИДАЦИЯ БЕЗОПАСНОСТИ ===
        validation = validate_deposit(
            chain=wallet_address.chain,
            token_contract=token_contract,
            amount=amount,
            asset=asset,
            from_address=from_address,
        )

        if not validation.is_valid:
            logger.warning(
                f"[SECURITY] Deposit REJECTED: {amount} {asset} on {wallet_address.chain} "
                f"reason={validation.reason} tx={tx_hash[:16]}..."
            )
            return None

        # Проверяем, не записан ли уже этот депозит
        stmt = select(Deposit).where(
            and_(
                Deposit.chain == wallet_address.chain,
                Deposit.tx_hash == tx_hash,
                Deposit.log_index == log_index,
            )
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            logger.debug(f"Deposit already recorded: {tx_hash}")
            return existing

        deposit = Deposit(
            id=uuid.uuid4(),
            user_wallet_id=wallet_address.user_wallet_id,
            wallet_address_id=wallet_address.id,
            chain=wallet_address.chain,
            tx_hash=tx_hash,
            block_number=block_number,
            log_index=log_index,
            amount=amount,
            asset=asset,
            token_contract=token_contract,
            from_address=from_address,
            status=DepositStatus.PENDING,
            confirmations=0,
            required_confirmations=required_confirmations,
            detected_at=datetime.now(timezone.utc),
        )

        self.session.add(deposit)
        await self.session.commit()

        logger.info(
            f"Deposit recorded: {amount} {asset} on {wallet_address.chain} "
            f"(tx={tx_hash[:16]}...)"
        )

        return deposit

    async def update_deposit_confirmations(
        self,
        deposit: Deposit,
        current_block: int,
    ) -> bool:
        """
        Обновить количество подтверждений депозита.

        Args:
            deposit: Депозит
            current_block: Текущий блок в сети

        Returns:
            True если депозит подтверждён
        """
        confirmations = current_block - deposit.block_number + 1
        deposit.confirmations = confirmations

        if deposit.status == DepositStatus.PENDING:
            deposit.status = DepositStatus.CONFIRMING

        if confirmations >= deposit.required_confirmations:
            if deposit.status != DepositStatus.CONFIRMED:
                deposit.status = DepositStatus.CONFIRMED
                deposit.confirmed_at = datetime.now(timezone.utc)

                # Зачисляем на баланс
                await self._credit_balance(deposit)

                logger.info(
                    f"Deposit confirmed: {deposit.amount} {deposit.asset} "
                    f"(tx={deposit.tx_hash[:16]}...)"
                )
                return True

        await self.session.commit()
        return False

    async def _credit_balance(self, deposit: Deposit) -> None:
        """
        Зачислить депозит на баланс пользователя.

        ВАЖНО: Проверяем credited_at для защиты от double-crediting!
        """
        # === ЗАЩИТА ОТ DOUBLE-CREDIT ===
        # Проверяем, не был ли депозит уже зачислен
        if deposit.credited_at is not None:
            logger.warning(
                f"[SECURITY] Double-credit attempt prevented! "
                f"Deposit {deposit.tx_hash[:16]}... already credited at {deposit.credited_at}"
            )
            return

        # Находим или создаём баланс
        stmt = select(UserBalance).where(
            and_(
                UserBalance.user_wallet_id == deposit.user_wallet_id,
                UserBalance.asset == deposit.asset,
            )
        )
        result = await self.session.execute(stmt)
        balance = result.scalar_one_or_none()

        if not balance:
            balance = UserBalance(
                id=uuid.uuid4(),
                user_wallet_id=deposit.user_wallet_id,
                asset=deposit.asset,
                balance=Decimal("0"),
                total_deposited=Decimal("0"),
                total_withdrawn=Decimal("0"),
            )
            self.session.add(balance)

        # Обновляем баланс
        balance.balance += deposit.amount
        balance.total_deposited += deposit.amount

        # Помечаем как зачисленный (ДО коммита для атомарности)
        deposit.credited_at = datetime.now(timezone.utc)

        await self.session.commit()

        logger.info(
            f"Balance credited: +{deposit.amount} {deposit.asset}, "
            f"new balance: {balance.balance}"
        )

    async def get_user_balances(
        self,
        merchant_id: uuid.UUID,
        external_user_id: str,
    ) -> dict[str, Decimal]:
        """
        Получить балансы пользователя.

        Returns:
            Словарь asset -> balance
        """
        wallet = await self.get_wallet(merchant_id, external_user_id)
        if not wallet:
            return {}

        return {balance.asset: balance.balance for balance in wallet.balances}

    async def get_deposit_history(
        self,
        merchant_id: uuid.UUID,
        external_user_id: str,
        limit: int = 50,
    ) -> list[Deposit]:
        """Получить историю депозитов пользователя."""
        wallet = await self.get_wallet(merchant_id, external_user_id)
        if not wallet:
            return []

        stmt = (
            select(Deposit)
            .where(Deposit.user_wallet_id == wallet.id)
            .order_by(Deposit.detected_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_deposits(
        self,
        merchant_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        chain: str | None = None,
        since: datetime | None = None,
    ) -> tuple[list[Deposit], int]:
        """
        Получить все депозиты мерчанта (по всем пользователям).

        Args:
            merchant_id: ID мерчанта
            limit: Максимум записей
            offset: Смещение для пагинации
            status: Фильтр по статусу (pending, confirmed)
            chain: Фильтр по сети
            since: Только депозиты после этой даты

        Returns:
            Кортеж (список депозитов, общее количество)
        """
        from sqlalchemy import func

        # Базовый запрос - джойним с wallet чтобы фильтровать по merchant
        base_conditions = [
            UserWallet.merchant_id == merchant_id,
        ]

        if status:
            try:
                status_enum = DepositStatus(status)
                base_conditions.append(Deposit.status == status_enum)
            except ValueError:
                pass  # Игнорируем невалидный статус

        if chain:
            base_conditions.append(Deposit.chain == chain)

        if since:
            base_conditions.append(Deposit.detected_at >= since)

        # Подсчёт общего количества
        count_stmt = (
            select(func.count(Deposit.id))
            .join(UserWallet, Deposit.user_wallet_id == UserWallet.id)
            .where(and_(*base_conditions))
        )
        total = await self.session.scalar(count_stmt) or 0

        # Получение депозитов с информацией о пользователе
        stmt = (
            select(Deposit)
            .join(UserWallet, Deposit.user_wallet_id == UserWallet.id)
            .options(selectinload(Deposit.user_wallet))
            .where(and_(*base_conditions))
            .order_by(Deposit.detected_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        deposits = list(result.scalars().all())

        return deposits, total


async def get_all_active_wallet_addresses(
    session: AsyncSession,
    chain: str,
) -> dict[str, WalletAddress]:
    """
    Получить все активные wallet addresses для сети.

    Returns:
        Словарь address -> WalletAddress
    """
    stmt = (
        select(WalletAddress)
        .options(selectinload(WalletAddress.user_wallet))
        .where(
            and_(
                WalletAddress.chain == chain,
                WalletAddress.is_active == True,
            )
        )
    )
    result = await session.execute(stmt)
    addresses = result.scalars().all()

    return {addr.address.lower(): addr for addr in addresses}
