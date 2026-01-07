"""
HD Wallet для генерации депозитных адресов.

Используется BIP-44 derivation path:
m/44'/60'/0'/0/index

где:
- 44' = BIP-44
- 60' = Ethereum (и все EVM совместимые сети)
- 0' = account
- 0 = external chain
- index = порядковый номер адреса
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Tuple

from eth_account import Account
from mnemonic import Mnemonic

# BIP-44 constants
BIP44_PURPOSE = 44
ETHEREUM_COIN_TYPE = 60
HARDENED_OFFSET = 0x80000000

# Derivation path template
DERIVATION_PATH_TEMPLATE = "m/44'/60'/0'/0/{index}"


@dataclass
class DerivedKey:
    """Результат деривации ключа."""

    address: str  # Ethereum address (0x...)
    private_key: str  # Private key (0x...)
    derivation_path: str  # Full derivation path
    index: int  # Derivation index


class HDWallet:
    """
    HD Wallet для генерации детерминированных адресов.

    Использует BIP-39 мнемонику и BIP-44 derivation paths.
    """

    def __init__(self, mnemonic: str) -> None:
        """
        Инициализация кошелька.

        Args:
            mnemonic: BIP-39 мнемоническая фраза (12 или 24 слова)
        """
        self.mnemonic = mnemonic

        # Валидация мнемоники
        m = Mnemonic("english")
        if not m.check(mnemonic):
            raise ValueError("Invalid mnemonic phrase")

        # Генерируем seed из мнемоники
        self.seed = m.to_seed(mnemonic)

        # Деривируем master key
        self._master_key, self._master_chain_code = self._derive_master_key()

    def _derive_master_key(self) -> Tuple[bytes, bytes]:
        """
        Деривировать master key из seed (BIP-32).

        Returns:
            Tuple[private_key, chain_code]
        """
        # HMAC-SHA512 с ключом "Bitcoin seed"
        h = hmac.new(b"Bitcoin seed", self.seed, hashlib.sha512).digest()
        return h[:32], h[32:]

    def _derive_child_key(
        self,
        parent_key: bytes,
        parent_chain_code: bytes,
        index: int,
        hardened: bool = False,
    ) -> Tuple[bytes, bytes]:
        """
        Деривировать дочерний ключ (BIP-32).

        Args:
            parent_key: Родительский приватный ключ
            parent_chain_code: Родительский chain code
            index: Индекс деривации
            hardened: Использовать hardened derivation

        Returns:
            Tuple[child_key, child_chain_code]
        """
        if hardened:
            index += HARDENED_OFFSET
            data = b"\x00" + parent_key + index.to_bytes(4, "big")
        else:
            # Для non-hardened нужен публичный ключ
            account = Account.from_key(parent_key)
            public_key = bytes.fromhex(
                account._key_obj.public_key.to_compressed_bytes().hex()
            )
            data = public_key + index.to_bytes(4, "big")

        h = hmac.new(parent_chain_code, data, hashlib.sha512).digest()

        # child_key = (parent_key + h[:32]) mod n
        # где n - порядок группы secp256k1
        n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        child_key_int = (
            int.from_bytes(parent_key, "big") + int.from_bytes(h[:32], "big")
        ) % n
        child_key = child_key_int.to_bytes(32, "big")
        child_chain_code = h[32:]

        return child_key, child_chain_code

    def derive_key(self, index: int) -> DerivedKey:
        """
        Деривировать ключ по индексу.

        Использует путь: m/44'/60'/0'/0/{index}

        Args:
            index: Порядковый номер адреса (0, 1, 2, ...)

        Returns:
            DerivedKey с адресом и приватным ключом
        """
        # m/44' (hardened)
        key, chain_code = self._derive_child_key(
            self._master_key, self._master_chain_code, BIP44_PURPOSE, hardened=True
        )

        # m/44'/60' (hardened)
        key, chain_code = self._derive_child_key(
            key, chain_code, ETHEREUM_COIN_TYPE, hardened=True
        )

        # m/44'/60'/0' (hardened) - account
        key, chain_code = self._derive_child_key(key, chain_code, 0, hardened=True)

        # m/44'/60'/0'/0 - external chain (non-hardened)
        key, chain_code = self._derive_child_key(key, chain_code, 0, hardened=False)

        # m/44'/60'/0'/0/{index} - address (non-hardened)
        key, chain_code = self._derive_child_key(key, chain_code, index, hardened=False)

        # Создаём account из приватного ключа
        account = Account.from_key(key)

        return DerivedKey(
            address=account.address,
            private_key="0x" + key.hex(),
            derivation_path=DERIVATION_PATH_TEMPLATE.format(index=index),
            index=index,
        )

    @staticmethod
    def generate_mnemonic(strength: int = 256) -> str:
        """
        Сгенерировать новую мнемоническую фразу.

        Args:
            strength: Количество бит энтропии (128 = 12 слов, 256 = 24 слова)

        Returns:
            Мнемоническая фраза
        """
        m = Mnemonic("english")
        return m.generate(strength)

    @staticmethod
    def validate_mnemonic(mnemonic: str) -> bool:
        """Проверить валидность мнемонической фразы."""
        m = Mnemonic("english")
        return m.check(mnemonic)


def derive_address(mnemonic: str, index: int) -> DerivedKey:
    """
    Удобная функция для деривации одного адреса.

    Args:
        mnemonic: BIP-39 мнемоническая фраза
        index: Порядковый номер адреса

    Returns:
        DerivedKey с адресом и приватным ключом
    """
    wallet = HDWallet(mnemonic)
    return wallet.derive_key(index)
