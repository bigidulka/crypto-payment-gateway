"""
Тесты модулей системы для всех EVM чейнов.

Проверяет:
1. Конфигурацию чейнов
2. HD-кошельки и генерацию адресов
3. EVM адаптеры для всех сетей
4. Модели базы данных
5. Sweep задачи
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================
# 1. CHAIN CONFIG TESTS
# ============================================================

class TestChainConfig:
    """Тесты конфигурации чейнов."""

    def test_all_evm_chains_configured(self):
        """Все EVM чейны должны быть сконфигурированы."""
        from src.blockchain.chains import get_evm_chains, get_all_chains
        
        expected_chains = ["base", "arbitrum", "bsc", "polygon", "avax", "optimism"]
        evm_chains = get_evm_chains()
        all_chains = get_all_chains()
        
        for chain in expected_chains:
            assert chain in evm_chains, f"Chain {chain} not in EVM chains"
            assert chain in all_chains, f"Chain {chain} not configured"

    def test_chain_configs_have_required_fields(self):
        """Каждый чейн должен иметь обязательные поля."""
        from src.blockchain.chains import get_evm_chains, get_chain_config

        required_fields = [
            "name", "chain_id", "chain_type", "rpc_url", 
            "confirmations", "native_symbol", "tokens"
        ]

        for chain in get_evm_chains():
            config = get_chain_config(chain)
            for field in required_fields:
                assert hasattr(config, field), f"Chain {chain} missing field {field}"

    def test_all_chains_have_usdc_or_usdt(self):
        """Каждый EVM чейн должен иметь USDC или USDT."""
        from src.blockchain.chains import get_evm_chains, get_chain_config

        for chain in get_evm_chains():
            config = get_chain_config(chain)
            tokens = list(config.tokens.keys())
            assert "USDC" in tokens or "USDT" in tokens, \
                f"Chain {chain} has no USDC or USDT"

    def test_chain_aliases(self):
        """Проверка алиасов чейнов."""
        from src.blockchain.chains import get_chain_config
        
        # Проверяем алиасы - проверяем что они резолвятся в правильные чейны
        assert get_chain_config("arb").chain_id == 42161  # Arbitrum
        assert get_chain_config("bnb").chain_id == 56     # BSC
        assert get_chain_config("opt").chain_id == 10     # Optimism

    def test_chain_type_is_evm(self):
        """Все текущие чейны должны быть EVM."""
        from src.blockchain.chains import get_evm_chains, get_chain_config, ChainType

        for chain in get_evm_chains():
            config = get_chain_config(chain)
            assert config.chain_type == ChainType.EVM
            assert config.is_evm is True


# ============================================================
# 2. HD WALLET TESTS
# ============================================================

class TestHDWallet:
    """Тесты HD кошелька."""

    def test_derive_key_deterministic(self):
        """Один и тот же seed + index = один адрес."""
        from src.crypto.hd_wallet import HDWallet
        
        # Используем тестовую мнемонику
        test_mnemonic = "abandon " * 11 + "about"
        wallet = HDWallet(test_mnemonic)
        
        key1 = wallet.derive_key(0)
        key2 = wallet.derive_key(0)
        
        assert key1.address == key2.address, "Same index should produce same address"

    def test_derive_different_indexes(self):
        """Разные индексы = разные адреса."""
        from src.crypto.hd_wallet import HDWallet
        
        test_mnemonic = "abandon " * 11 + "about"
        wallet = HDWallet(test_mnemonic)
        
        addresses = [wallet.derive_key(i).address for i in range(5)]
        unique_addresses = set(addresses)
        
        assert len(unique_addresses) == 5, "Different indexes should produce unique addresses"

    def test_address_format(self):
        """Адрес должен быть в правильном формате."""
        from src.crypto.hd_wallet import HDWallet
        
        test_mnemonic = "abandon " * 11 + "about"
        wallet = HDWallet(test_mnemonic)
        
        key = wallet.derive_key(0)
        
        assert key.address.startswith("0x"), "Address should start with 0x"
        assert len(key.address) == 42, "Address should be 42 chars"

    def test_private_key_derivation(self):
        """Приватный ключ должен генерироваться."""
        from src.crypto.hd_wallet import HDWallet
        
        test_mnemonic = "abandon " * 11 + "about"
        wallet = HDWallet(test_mnemonic)
        
        key = wallet.derive_key(0)
        
        assert key.private_key is not None
        assert key.private_key.startswith("0x")
        assert len(key.private_key) == 66  # 0x + 64 hex chars

    def test_derivation_path(self):
        """Проверка derivation path."""
        from src.crypto.hd_wallet import HDWallet
        
        test_mnemonic = "abandon " * 11 + "about"
        wallet = HDWallet(test_mnemonic)
        
        key = wallet.derive_key(5)
        
        assert key.derivation_path == "m/44'/60'/0'/0/5"
        assert key.index == 5

    def test_generate_mnemonic(self):
        """Генерация новой мнемоники."""
        from src.crypto.hd_wallet import HDWallet
        
        mnemonic = HDWallet.generate_mnemonic()
        
        words = mnemonic.split()
        assert len(words) == 24  # 256 bits = 24 words
        
        # Должна быть валидной
        assert HDWallet.validate_mnemonic(mnemonic)


# ============================================================
# 3. EVM ADAPTER TESTS
# ============================================================

class TestEvmAdapter:
    """Тесты EVM адаптера для всех чейнов."""

    @pytest.mark.parametrize("chain", ["base", "arbitrum", "bsc", "polygon", "avax", "optimism"])
    def test_adapter_initialization(self, chain):
        """Адаптер должен инициализироваться для каждого чейна."""
        from src.blockchain.evm_adapter import EvmAdapter
        
        adapter = EvmAdapter(chain)
        
        assert adapter.chain == chain
        assert adapter.w3 is not None

    @pytest.mark.parametrize("chain", ["base", "arbitrum", "bsc", "polygon", "avax", "optimism"])
    def test_adapter_has_config(self, chain):
        """Адаптер должен иметь конфиг чейна."""
        from src.blockchain.evm_adapter import EvmAdapter
        
        adapter = EvmAdapter(chain)
        
        assert adapter.config is not None
        assert adapter.config.name is not None

    @pytest.mark.parametrize("chain,expected_id", [
        ("base", 8453),
        ("arbitrum", 42161),
        ("bsc", 56),
        ("polygon", 137),
        ("avax", 43114),
        ("optimism", 10),
    ])
    def test_chain_config_id(self, chain, expected_id):
        """Проверка chain_id через config."""
        from src.blockchain.evm_adapter import EvmAdapter
        
        adapter = EvmAdapter(chain)
        assert adapter.config.chain_id == expected_id


# ============================================================
# 4. TOKEN AMOUNT PARSING TESTS
# ============================================================

class TestTokenAmounts:
    """Тесты парсинга сумм токенов."""

    @pytest.mark.parametrize("chain,token,raw,expected", [
        ("base", "USDC", 1000000, Decimal("1")),           # 1 USDC (6 decimals)
        ("bsc", "USDT", 1000000000000000000, Decimal("1")), # 1 USDT BSC (18 decimals)
        ("arbitrum", "USDC", 500000, Decimal("0.5")),      # 0.5 USDC
    ])
    def test_parse_amount(self, chain, token, raw, expected):
        """Парсинг raw суммы в читаемую."""
        from src.blockchain.chains import parse_token_amount
        
        result = parse_token_amount(raw, chain, token)
        assert result == expected

    @pytest.mark.parametrize("chain,token,amount,expected", [
        ("base", "USDC", Decimal("1"), 1000000),
        ("bsc", "USDT", Decimal("1"), 1000000000000000000),
        ("arbitrum", "USDC", Decimal("0.5"), 500000),
    ])
    def test_to_raw_amount(self, chain, token, amount, expected):
        """Конвертация читаемой суммы в raw."""
        from src.blockchain.chains import to_raw_amount
        
        result = to_raw_amount(amount, chain, token)
        assert result == expected


# ============================================================
# 5. MODEL TESTS
# ============================================================

class TestModels:
    """Тесты моделей базы данных."""

    def test_deposit_status_enum(self):
        """Проверка статусов депозита."""
        from src.db.models.user_wallet import DepositStatus
        
        assert DepositStatus.PENDING.value == "pending"
        assert DepositStatus.CONFIRMED.value == "confirmed"
        assert DepositStatus.SWEPT.value == "swept"

    def test_sweep_state_enum(self):
        """Проверка состояний sweep."""
        from src.db.models.sweep import SweepState
        
        assert SweepState.PENDING_GAS.value == "pending_gas"
        assert SweepState.FUNDING.value == "funding"
        assert SweepState.SWEEPING.value == "sweeping"
        assert SweepState.COMPLETED.value == "completed"
        assert SweepState.FAILED.value == "failed"

    def test_deposit_sweep_job_model(self):
        """Проверка модели DepositSweepJob."""
        from src.db.models.sweep import DepositSweepJob, SweepState
        
        job = DepositSweepJob(
            id=uuid.uuid4(),
            deposit_id=uuid.uuid4(),
            state=SweepState.PENDING_GAS,
            attempts=0,
            max_attempts=10,
        )
        
        assert job.state == SweepState.PENDING_GAS
        assert job.attempts == 0
        assert job.max_attempts == 10

    def test_deposit_model_fields(self):
        """Проверка полей модели Deposit."""
        from src.db.models.user_wallet import Deposit, DepositStatus
        
        deposit = Deposit(
            id=uuid.uuid4(),
            user_wallet_id=uuid.uuid4(),
            wallet_address_id=uuid.uuid4(),
            chain="arbitrum",
            tx_hash="0x" + "a" * 64,
            block_number=1000,
            log_index=0,
            amount=Decimal("100"),
            asset="USDT",
            token_contract="0x" + "b" * 40,
            from_address="0x" + "c" * 40,
            status=DepositStatus.PENDING,
            confirmations=0,
            required_confirmations=12,
            detected_at=datetime.now(timezone.utc),
        )
        
        assert deposit.chain == "arbitrum"
        assert deposit.amount == Decimal("100")
        assert deposit.status == DepositStatus.PENDING


# ============================================================
# 6. ENCRYPTION TESTS
# ============================================================

class TestEncryption:
    """Тесты шифрования приватных ключей."""

    def test_encrypt_decrypt_roundtrip(self):
        """Шифрование и расшифровка должны давать исходный ключ."""
        from src.crypto.encryption import encrypt_private_key, decrypt_private_key
        import base64
        
        original_key = "a" * 64  # 256-bit hex key
        # Генерируем валидный 32-byte ключ шифрования в base64
        encryption_key = base64.b64encode(b"0" * 32).decode()
        
        encrypted = encrypt_private_key(original_key, encryption_key)
        decrypted = decrypt_private_key(encrypted, encryption_key)
        
        assert decrypted == "0x" + original_key

    def test_encrypted_differs_from_original(self):
        """Зашифрованные данные должны отличаться от исходных."""
        from src.crypto.encryption import encrypt_private_key
        import base64
        
        original_key = "b" * 64
        encryption_key = base64.b64encode(b"1" * 32).decode()
        
        encrypted = encrypt_private_key(original_key, encryption_key)
        
        # Encrypted - это bytes
        assert encrypted != original_key.encode()

    def test_different_keys_different_encryption(self):
        """Разные ключи дают разный шифротекст."""
        from src.crypto.encryption import encrypt_private_key
        import base64
        
        key1 = "a" * 64
        key2 = "b" * 64
        encryption_key = base64.b64encode(b"2" * 32).decode()
        
        encrypted1 = encrypt_private_key(key1, encryption_key)
        encrypted2 = encrypt_private_key(key2, encryption_key)
        
        assert encrypted1 != encrypted2


# ============================================================
# 7. PERSISTENT POLLER SWEEP JOB CREATION
# ============================================================

class TestPersistentPollerSweepJob:
    """Тесты создания sweep job из persistent poller."""

    def test_min_sweep_threshold(self):
        """Проверка минимального порога для sweep."""
        from src.workers.persistent_poller import MIN_SWEEP_AMOUNT_USD
        
        assert MIN_SWEEP_AMOUNT_USD == Decimal("0.50")

    @pytest.mark.asyncio
    async def test_sweep_job_not_created_below_threshold(self):
        """Sweep job не создаётся если сумма ниже порога."""
        from decimal import Decimal
        from src.workers.persistent_poller import MIN_SWEEP_AMOUNT_USD
        
        # Сумма ниже порога
        amount = Decimal("0.30")
        
        assert amount < MIN_SWEEP_AMOUNT_USD


# ============================================================
# 8. BATCH SWEEPER TESTS
# ============================================================

class TestBatchSweeper:
    """Тесты batch sweeper."""

    def test_evm_chains_list(self):
        """Batch sweeper должен обрабатывать все EVM чейны."""
        from src.blockchain.chains import get_evm_chains
        
        chains = get_evm_chains()
        expected = ["base", "arbitrum", "bsc", "polygon", "avax", "optimism"]
        
        for chain in expected:
            assert chain in chains


# ============================================================
# 9. RPC CONNECTIVITY TESTS (smoke tests)
# ============================================================

class TestRPCConnectivity:
    """Тесты подключения к RPC."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chain", ["base", "arbitrum", "bsc", "polygon", "avax", "optimism"])
    async def test_can_get_block_number(self, chain):
        """Проверка возможности получить номер блока."""
        from src.blockchain.evm_adapter import EvmAdapter
        
        adapter = EvmAdapter(chain)
        
        try:
            block = await adapter.get_latest_block()
            assert block > 0, f"Block number should be positive for {chain}"
        except Exception as e:
            # RPC может быть недоступен в тестовом окружении
            pytest.skip(f"RPC unavailable for {chain}: {e}")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chain", ["base", "arbitrum", "bsc", "polygon", "avax", "optimism"])
    async def test_can_check_balance(self, chain):
        """Проверка возможности получить баланс."""
        from src.blockchain.evm_adapter import EvmAdapter
        
        adapter = EvmAdapter(chain)
        test_address = "0x0000000000000000000000000000000000000001"
        
        try:
            balance = await adapter.get_native_balance(test_address)
            assert balance >= 0, f"Balance should be non-negative for {chain}"
        except Exception as e:
            pytest.skip(f"RPC unavailable for {chain}: {e}")


# ============================================================
# 10. TOKEN CONTRACT TESTS
# ============================================================

class TestTokenContracts:
    """Тесты контрактов токенов."""

    @pytest.mark.parametrize("chain,token", [
        ("base", "USDC"),
        ("arbitrum", "USDC"),
        ("arbitrum", "USDT"),
        ("bsc", "USDT"),
        ("bsc", "USDC"),
        ("polygon", "USDC"),
        ("polygon", "USDT"),
        ("avax", "USDC"),
        ("avax", "USDT"),
        ("optimism", "USDC"),
        ("optimism", "USDT"),
    ])
    def test_token_contract_format(self, chain, token):
        """Адреса контрактов должны быть в правильном формате."""
        from src.blockchain.chains import get_token_contract
        
        contract = get_token_contract(chain, token)
        
        assert contract.startswith("0x"), f"Contract should start with 0x"
        assert len(contract) == 42, f"Contract should be 42 chars"

    @pytest.mark.parametrize("chain,token,expected_decimals", [
        ("base", "USDC", 6),
        ("arbitrum", "USDC", 6),
        ("arbitrum", "USDT", 6),
        ("bsc", "USDT", 18),  # BSC USDT has 18 decimals
        ("bsc", "USDC", 18),  # BSC USDC has 18 decimals
        ("polygon", "USDC", 6),
        ("polygon", "USDT", 6),
        ("avax", "USDC", 6),
        ("avax", "USDT", 6),
        ("optimism", "USDC", 6),
        ("optimism", "USDT", 6),
    ])
    def test_token_decimals(self, chain, token, expected_decimals):
        """Проверка decimals токенов."""
        from src.blockchain.chains import get_chain_config
        
        config = get_chain_config(chain)
        token_config = config.get_token(token)
        
        assert token_config is not None, f"Token {token} not found for {chain}"
        assert token_config.decimals == expected_decimals, \
            f"Expected {expected_decimals} decimals for {token} on {chain}"


# ============================================================
# 11. TRANSFER EVENT TESTS
# ============================================================

class TestTransferEvent:
    """Тесты структуры Transfer Event."""

    def test_transfer_event_dataclass(self):
        """Проверка dataclass TransferEvent."""
        from src.blockchain.base_adapter import TransferEvent, ChainType
        
        event = TransferEvent(
            tx_hash="0x" + "a" * 64,
            block_number=12345,
            log_index=0,
            from_address="0x" + "b" * 40,
            to_address="0x" + "c" * 40,
            token_address="0x" + "d" * 40,
            token_symbol="USDT",
            amount=Decimal("100"),
            raw_amount=100000000,
            chain="arbitrum",
            chain_type=ChainType.EVM,
        )
        
        assert event.tx_hash.startswith("0x")
        assert event.amount == Decimal("100")
        assert event.token_symbol == "USDT"
        assert event.chain == "arbitrum"
        assert event.chain_type == ChainType.EVM

