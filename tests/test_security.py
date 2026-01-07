"""
Тесты безопасности для защиты от атак.
"""

import pytest
from decimal import Decimal

from src.core.deposit_security import validate_deposit, ValidationResult


class TestDepositSecurity:
    """Тесты валидации депозитов."""

    def test_zero_amount_rejected(self):
        """Нулевая сумма должна быть отклонена."""
        result = validate_deposit(
            chain="arbitrum",
            token_contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
            amount=Decimal("0"),
            asset="USDT",
        )
        assert not result.is_valid
        assert result.reason == "zero_amount"

    def test_negative_amount_rejected(self):
        """Отрицательная сумма должна быть отклонена."""
        result = validate_deposit(
            chain="arbitrum",
            token_contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
            amount=Decimal("-10"),
            asset="USDT",
        )
        assert not result.is_valid
        assert result.reason == "zero_amount"

    def test_dust_amount_rejected(self):
        """Микро-сумма (пыль) должна быть отклонена."""
        result = validate_deposit(
            chain="arbitrum",
            token_contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
            amount=Decimal("0.001"),  # меньше минимума 0.01
            asset="USDT",
        )
        assert not result.is_valid
        assert result.reason == "below_minimum"

    def test_valid_amount_accepted(self):
        """Нормальная сумма должна быть принята."""
        result = validate_deposit(
            chain="arbitrum",
            token_contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
            amount=Decimal("10.50"),
            asset="USDT",
        )
        assert result.is_valid
        assert result.reason is None

    def test_fake_token_contract_rejected(self):
        """Фейковый токен (не из конфига) должен быть отклонён."""
        result = validate_deposit(
            chain="arbitrum",
            token_contract="0x1234567890123456789012345678901234567890",  # фейковый
            amount=Decimal("100"),
            asset="USDT",
        )
        assert not result.is_valid
        assert result.reason == "unknown_token_contract"

    def test_asset_mismatch_rejected(self):
        """Токен USDT контракт но заявлен как USDC должен быть отклонён."""
        result = validate_deposit(
            chain="arbitrum",
            token_contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
            amount=Decimal("100"),
            asset="USDC",  # заявляем как USDC
        )
        assert not result.is_valid
        assert result.reason == "asset_mismatch"

    def test_case_insensitive_token_contract(self):
        """Адрес токена должен валидироваться без учёта регистра."""
        # Адрес USDT на Arbitrum в разных регистрах
        result_lower = validate_deposit(
            chain="arbitrum",
            token_contract="0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",
            amount=Decimal("10"),
            asset="USDT",
        )
        result_upper = validate_deposit(
            chain="arbitrum",
            token_contract="0xFD086BC7CD5C481DCC9C85EBE478A1C0B69FCBB9",
            amount=Decimal("10"),
            asset="USDT",
        )
        assert result_lower.is_valid
        assert result_upper.is_valid


class TestDoubleCredit:
    """Тесты защиты от двойного зачисления."""

    @pytest.mark.asyncio
    async def test_double_credit_prevented(self, test_session):
        """Депозит не должен зачисляться дважды."""
        from datetime import datetime, timezone
        from decimal import Decimal
        import uuid

        from src.db.models import (
            Deposit,
            DepositStatus,
            UserBalance,
            UserWallet,
            WalletAddress,
            Merchant,
        )
        from src.services.user_wallet_service import UserWalletService

        # Создаём мерчанта
        merchant = Merchant(
            id=uuid.uuid4(),
            name="Test Merchant",
            api_key_hash="test_hash",
            webhook_url="https://example.com/webhook",
        )
        test_session.add(merchant)

        # Создаём wallet
        wallet = UserWallet(
            id=uuid.uuid4(),
            merchant_id=merchant.id,
            external_user_id="user123",
        )
        test_session.add(wallet)

        # Создаём address
        wallet_address = WalletAddress(
            id=uuid.uuid4(),
            user_wallet_id=wallet.id,
            chain="arbitrum",
            address="0x" + "a" * 40,
            derivation_path="m/44'/60'/0'/0/0",
        )
        test_session.add(wallet_address)

        # Создаём депозит уже с credited_at
        deposit = Deposit(
            id=uuid.uuid4(),
            user_wallet_id=wallet.id,
            wallet_address_id=wallet_address.id,
            chain="arbitrum",
            tx_hash="0x" + "1" * 64,
            block_number=1000,
            log_index=0,
            amount=Decimal("100"),
            asset="USDT",
            token_contract="0x" + "b" * 40,
            from_address="0x" + "c" * 40,
            status=DepositStatus.CONFIRMED,
            confirmations=10,
            required_confirmations=10,
            detected_at=datetime.now(timezone.utc),
            confirmed_at=datetime.now(timezone.utc),
            credited_at=datetime.now(timezone.utc),  # Уже зачислен!
        )
        test_session.add(deposit)

        # Создаём баланс
        balance = UserBalance(
            id=uuid.uuid4(),
            user_wallet_id=wallet.id,
            asset="USDT",
            balance=Decimal("100"),  # Уже есть 100
            total_deposited=Decimal("100"),
            total_withdrawn=Decimal("0"),
        )
        test_session.add(balance)
        await test_session.commit()

        # Пытаемся зачислить ещё раз
        service = UserWalletService(test_session)
        await service._credit_balance(deposit)

        # Баланс не должен измениться
        await test_session.refresh(balance)
        assert balance.balance == Decimal("100")
        assert balance.total_deposited == Decimal("100")


class TestUniqueConstraints:
    """Тесты уникальных ограничений в БД."""

    @pytest.mark.asyncio
    async def test_duplicate_deposit_rejected_by_db(self, test_session):
        """Дублирующий депозит должен отклоняться на уровне БД."""
        from datetime import datetime, timezone
        from decimal import Decimal
        import uuid

        from sqlalchemy.exc import IntegrityError

        from src.db.models import (
            Deposit,
            DepositStatus,
            UserWallet,
            WalletAddress,
            Merchant,
        )

        # Создаём мерчанта
        merchant = Merchant(
            id=uuid.uuid4(),
            name="Test Merchant",
            api_key_hash="test_hash2",
            webhook_url="https://example.com/webhook",
        )
        test_session.add(merchant)

        # Создаём wallet
        wallet = UserWallet(
            id=uuid.uuid4(),
            merchant_id=merchant.id,
            external_user_id="user456",
        )
        test_session.add(wallet)

        # Создаём address
        wallet_address = WalletAddress(
            id=uuid.uuid4(),
            user_wallet_id=wallet.id,
            chain="arbitrum",
            address="0x" + "d" * 40,
            derivation_path="m/44'/60'/0'/0/1",
        )
        test_session.add(wallet_address)
        await test_session.commit()

        tx_hash = "0x" + "2" * 64

        # Первый депозит - ОК
        deposit1 = Deposit(
            id=uuid.uuid4(),
            user_wallet_id=wallet.id,
            wallet_address_id=wallet_address.id,
            chain="arbitrum",
            tx_hash=tx_hash,
            block_number=1000,
            log_index=0,  # Тот же log_index
            amount=Decimal("50"),
            asset="USDT",
            token_contract="0x" + "e" * 40,
            from_address="0x" + "f" * 40,
            status=DepositStatus.PENDING,
            confirmations=0,
            required_confirmations=10,
            detected_at=datetime.now(timezone.utc),
        )
        test_session.add(deposit1)
        await test_session.commit()

        # Второй депозит с тем же tx_hash и log_index - должен упасть
        deposit2 = Deposit(
            id=uuid.uuid4(),
            user_wallet_id=wallet.id,
            wallet_address_id=wallet_address.id,
            chain="arbitrum",
            tx_hash=tx_hash,  # Тот же!
            block_number=1000,
            log_index=0,  # Тот же!
            amount=Decimal("50"),
            asset="USDT",
            token_contract="0x" + "e" * 40,
            from_address="0x" + "f" * 40,
            status=DepositStatus.PENDING,
            confirmations=0,
            required_confirmations=10,
            detected_at=datetime.now(timezone.utc),
        )
        test_session.add(deposit2)

        with pytest.raises(IntegrityError):
            await test_session.commit()
