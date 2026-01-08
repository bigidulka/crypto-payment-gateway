"""
Тесты для non-EVM адаптеров (Solana, TON).

Запуск:
    pytest tests/test_non_evm.py -v
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

from src.blockchain.chains import (
    get_chain_config,
    get_all_chains,
    get_evm_chains,
    get_non_evm_chains,
    ChainType,
)


class TestChainConfig:
    """Тесты конфигурации сетей."""

    def test_solana_config(self):
        """Проверить конфигурацию Solana."""
        config = get_chain_config("solana")

        assert config.name == "Solana"
        assert config.chain_type == ChainType.SOLANA
        assert config.native_symbol == "SOL"
        assert config.native_decimals == 9
        assert config.address_length == 44
        assert config.confirmations == 32

        # Токены
        usdt = config.get_token("USDT")
        assert usdt is not None
        assert usdt.contract_address == "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
        assert usdt.decimals == 6

        usdc = config.get_token("USDC")
        assert usdc is not None
        assert usdc.contract_address == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def test_ton_config(self):
        """Проверить конфигурацию TON."""
        config = get_chain_config("ton")

        assert config.name == "The Open Network"
        assert config.chain_type == ChainType.TON
        assert config.native_symbol == "TON"
        assert config.native_decimals == 9
        assert config.address_length == 48
        assert config.confirmations == 12

        # Токены
        usdt = config.get_token("USDT")
        assert usdt is not None
        assert usdt.contract_address == "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

    def test_get_all_chains_includes_non_evm(self):
        """Проверить что get_all_chains включает non-EVM."""
        chains = get_all_chains()

        assert "solana" in chains
        assert "ton" in chains
        assert "base" in chains  # EVM тоже есть

    def test_get_evm_chains(self):
        """Проверить get_evm_chains."""
        evm_chains = get_evm_chains()

        assert "base" in evm_chains
        assert "arbitrum" in evm_chains
        assert "solana" not in evm_chains
        assert "ton" not in evm_chains

    def test_get_non_evm_chains(self):
        """Проверить get_non_evm_chains."""
        non_evm = get_non_evm_chains()

        assert "solana" in non_evm
        assert "ton" in non_evm
        assert "base" not in non_evm

    def test_is_evm_property(self):
        """Проверить is_evm свойство."""
        base_config = get_chain_config("base")
        solana_config = get_chain_config("solana")

        assert base_config.is_evm is True
        assert solana_config.is_evm is False


class TestSolanaAdapter:
    """Тесты Solana адаптера."""

    @pytest.fixture
    def adapter(self):
        """Создать адаптер."""
        from src.blockchain.solana_adapter import SolanaAdapter
        return SolanaAdapter()

    def test_chain_type(self, adapter):
        """Проверить тип сети."""
        assert adapter.chain_type == ChainType.SOLANA
        assert adapter.native_symbol == "SOL"
        assert adapter.address_length == 44

    def test_is_valid_address(self, adapter):
        """Проверить валидацию адресов."""
        # Валидные адреса (32 bytes base58)
        assert adapter.is_valid_address(
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        ) is True
        assert adapter.is_valid_address(
            "So11111111111111111111111111111111111111112"
        ) is True

        # Невалидные адреса
        assert adapter.is_valid_address("not_valid") is False
        assert adapter.is_valid_address("0x123") is False
        assert adapter.is_valid_address("") is False

    @pytest.mark.asyncio
    async def test_get_latest_block(self, adapter):
        """Проверить получение слота."""
        with patch.object(adapter, "_rpc_call", new_callable=AsyncMock) as mock:
            mock.return_value = 123456789

            slot = await adapter.get_latest_block()
            assert slot == 123456789
            mock.assert_called_once_with("getSlot")

    @pytest.mark.asyncio
    async def test_get_native_balance(self, adapter):
        """Проверить получение баланса SOL."""
        with patch.object(adapter, "_rpc_call", new_callable=AsyncMock) as mock:
            # 1 SOL = 1_000_000_000 lamports
            mock.return_value = {"value": 1_000_000_000}

            balance = await adapter.get_native_balance(
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            )

            assert balance == Decimal("1")

    @pytest.mark.asyncio
    async def test_is_tx_confirmed(self, adapter):
        """Проверить статус транзакции."""
        with patch.object(adapter, "_rpc_call", new_callable=AsyncMock) as mock:
            # Finalized transaction
            mock.return_value = {
                "value": [{"confirmationStatus": "finalized", "confirmations": None}]
            }

            confirmed = await adapter.is_tx_confirmed("signature123")
            assert confirmed is True

            # Pending transaction
            mock.return_value = {
                "value": [{"confirmationStatus": "confirmed", "confirmations": 10}]
            }

            confirmed = await adapter.is_tx_confirmed("signature123")
            assert confirmed is False  # 10 < 32


class TestTonAdapter:
    """Тесты TON адаптера."""

    @pytest.fixture
    def adapter(self):
        """Создать адаптер."""
        from src.blockchain.ton_adapter import TonAdapter
        return TonAdapter()

    def test_chain_type(self, adapter):
        """Проверить тип сети."""
        assert adapter.chain_type == ChainType.TON
        assert adapter.native_symbol == "TON"
        assert adapter.address_length == 48

    def test_is_valid_address_user_friendly(self, adapter):
        """Проверить валидацию user-friendly адресов."""
        # User-friendly формат (48 chars base64url)
        assert adapter.is_valid_address(
            "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
        ) is True

    def test_is_valid_address_raw(self, adapter):
        """Проверить валидацию raw адресов."""
        # Raw формат: workchain:hash
        assert adapter.is_valid_address(
            "0:b113a994b5024a16719f69139328eb759596c38a25f590a8b146fecdcb6220fd"
        ) is True

        # Masterchain
        assert adapter.is_valid_address(
            "-1:b113a994b5024a16719f69139328eb759596c38a25f590a8b146fecdcb6220fd"
        ) is True

    def test_is_valid_address_invalid(self, adapter):
        """Проверить отклонение невалидных адресов."""
        assert adapter.is_valid_address("not_valid") is False
        assert adapter.is_valid_address("0x123") is False
        assert adapter.is_valid_address("") is False

    @pytest.mark.asyncio
    async def test_get_latest_block(self, adapter):
        """Проверить получение seqno."""
        with patch.object(adapter, "_api_call", new_callable=AsyncMock) as mock:
            mock.return_value = {"last": {"seqno": 12345678}}

            seqno = await adapter.get_latest_block()
            assert seqno == 12345678

    @pytest.mark.asyncio
    async def test_get_native_balance(self, adapter):
        """Проверить получение баланса TON."""
        with patch.object(adapter, "_api_call", new_callable=AsyncMock) as mock:
            # 1 TON = 1_000_000_000 nanotons
            mock.return_value = "1000000000"

            balance = await adapter.get_native_balance(
                "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
            )

            assert balance == Decimal("1")


class TestSolanaHDWallet:
    """Тесты Solana HD Wallet."""

    @pytest.fixture
    def test_mnemonic(self):
        """Тестовая мнемоника (НЕ использовать в production!)."""
        return (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

    def test_create_wallet(self, test_mnemonic):
        """Проверить создание кошелька."""
        pytest.importorskip("nacl")
        from src.crypto.solana_wallet import SolanaHDWallet

        wallet = SolanaHDWallet(test_mnemonic)
        assert wallet is not None

    def test_derive_key(self, test_mnemonic):
        """Проверить деривацию ключа."""
        pytest.importorskip("nacl")
        from src.crypto.solana_wallet import SolanaHDWallet

        wallet = SolanaHDWallet(test_mnemonic)
        key = wallet.derive_key(0)

        assert key.address is not None
        assert len(key.address) in range(32, 45)  # Base58 encoded
        assert key.private_key is not None
        assert key.derivation_path == "m/44'/501'/0'/0'"
        assert key.index == 0

    def test_deterministic_derivation(self, test_mnemonic):
        """Проверить детерминированность деривации."""
        pytest.importorskip("nacl")
        from src.crypto.solana_wallet import SolanaHDWallet

        wallet1 = SolanaHDWallet(test_mnemonic)
        wallet2 = SolanaHDWallet(test_mnemonic)

        # Одинаковая мнемоника → одинаковые адреса
        assert wallet1.get_address(0) == wallet2.get_address(0)
        assert wallet1.get_address(1) == wallet2.get_address(1)

    def test_different_indexes(self, test_mnemonic):
        """Проверить разные адреса для разных индексов."""
        pytest.importorskip("nacl")
        from src.crypto.solana_wallet import SolanaHDWallet

        wallet = SolanaHDWallet(test_mnemonic)

        addr0 = wallet.get_address(0)
        addr1 = wallet.get_address(1)
        addr2 = wallet.get_address(2)

        # Все адреса разные
        assert addr0 != addr1
        assert addr1 != addr2
        assert addr0 != addr2


class TestTonHDWallet:
    """Тесты TON HD Wallet."""

    @pytest.fixture
    def test_mnemonic(self):
        """Тестовая мнемоника (НЕ использовать в production!)."""
        return (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

    def test_create_wallet(self, test_mnemonic):
        """Проверить создание кошелька."""
        pytest.importorskip("nacl")
        from src.crypto.ton_wallet import TonHDWallet

        wallet = TonHDWallet(test_mnemonic)
        assert wallet is not None

    def test_derive_key(self, test_mnemonic):
        """Проверить деривацию ключа."""
        pytest.importorskip("nacl")
        from src.crypto.ton_wallet import TonHDWallet

        wallet = TonHDWallet(test_mnemonic)
        key = wallet.derive_key(0)

        assert key.address is not None
        assert key.address_raw is not None
        assert key.public_key is not None
        assert key.private_key is not None
        assert key.derivation_path == "m/44'/607'/0'"
        assert key.index == 0

    def test_deterministic_derivation(self, test_mnemonic):
        """Проверить детерминированность деривации."""
        pytest.importorskip("nacl")
        from src.crypto.ton_wallet import TonHDWallet

        wallet1 = TonHDWallet(test_mnemonic)
        wallet2 = TonHDWallet(test_mnemonic)

        # Одинаковая мнемоника → одинаковые адреса
        assert wallet1.get_address(0) == wallet2.get_address(0)
        assert wallet1.get_address(1) == wallet2.get_address(1)


class TestBaseAdapter:
    """Тесты базового адаптера."""

    def test_transfer_event_dataclass(self):
        """Проверить TransferEvent dataclass."""
        from src.blockchain.base_adapter import TransferEvent, ChainType

        event = TransferEvent(
            tx_hash="hash123",
            block_number=12345,
            log_index=0,
            from_address="sender",
            to_address="receiver",
            token_address="token",
            token_symbol="USDT",
            amount=Decimal("100"),
            raw_amount=100_000_000,
            chain="solana",
            chain_type=ChainType.SOLANA,
            timestamp=1234567890,
        )

        assert event.tx_hash == "hash123"
        assert event.chain_type == ChainType.SOLANA
        assert event.amount == Decimal("100")

    def test_parse_amount(self):
        """Проверить преобразование amount."""
        from src.blockchain.base_adapter import BaseAdapter

        # Создаём mock адаптер для тестирования методов
        class MockAdapter(BaseAdapter):
            @property
            def chain_type(self):
                return ChainType.EVM

            @property
            def native_symbol(self):
                return "ETH"

            @property
            def address_length(self):
                return 42

            async def is_connected(self):
                return True

            async def get_latest_block(self):
                return 0

            async def get_block_timestamp(self, block):
                return None

            async def get_native_balance(self, address):
                return Decimal(0)

            async def get_token_balance(self, address, token):
                return Decimal(0)

            async def get_transfer_events(self, from_block, to_block, to_addresses, token_addresses=None):
                return []

            async def get_confirmations(self, tx_hash):
                return None

            async def is_tx_confirmed(self, tx_hash, required=None):
                return False

            async def estimate_transfer_fee(self, from_addr, to_addr, token=None, amount=None):
                return None

            async def send_native_token(self, privkey, to, amount):
                return None

            async def send_token(self, privkey, to, token, amount):
                return None

            def is_valid_address(self, address):
                return True

            def normalize_address(self, address):
                return address

        adapter = MockAdapter("test", None)

        # 6 decimals (USDT/USDC)
        assert adapter.parse_amount(1_000_000, 6) == Decimal("1")
        assert adapter.parse_amount(1_500_000, 6) == Decimal("1.5")

        # 9 decimals (SOL/TON)
        assert adapter.parse_amount(1_000_000_000, 9) == Decimal("1")

        # 18 decimals (ETH)
        assert adapter.parse_amount(10**18, 18) == Decimal("1")

    def test_to_raw_amount(self):
        """Проверить обратное преобразование."""
        from src.blockchain.base_adapter import BaseAdapter

        # Используем mock
        class MockAdapter(BaseAdapter):
            @property
            def chain_type(self):
                return ChainType.EVM

            @property
            def native_symbol(self):
                return "ETH"

            @property
            def address_length(self):
                return 42

            async def is_connected(self):
                return True

            async def get_latest_block(self):
                return 0

            async def get_block_timestamp(self, block):
                return None

            async def get_native_balance(self, address):
                return Decimal(0)

            async def get_token_balance(self, address, token):
                return Decimal(0)

            async def get_transfer_events(self, from_block, to_block, to_addresses, token_addresses=None):
                return []

            async def get_confirmations(self, tx_hash):
                return None

            async def is_tx_confirmed(self, tx_hash, required=None):
                return False

            async def estimate_transfer_fee(self, from_addr, to_addr, token=None, amount=None):
                return None

            async def send_native_token(self, privkey, to, amount):
                return None

            async def send_token(self, privkey, to, token, amount):
                return None

            def is_valid_address(self, address):
                return True

            def normalize_address(self, address):
                return address

        adapter = MockAdapter("test", None)

        # 6 decimals
        assert adapter.to_raw_amount(Decimal("1"), 6) == 1_000_000
        assert adapter.to_raw_amount(Decimal("1.5"), 6) == 1_500_000

        # 9 decimals
        assert adapter.to_raw_amount(Decimal("1"), 9) == 1_000_000_000
